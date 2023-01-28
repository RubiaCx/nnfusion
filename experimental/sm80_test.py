import memopt
import numpy as np
import tvm
from cutlass_intrin import *
from layout import *
from memopt.utils import CompileResult
from tvm import te

tvm.register_func("tvm_callback_cuda_compile", override=True)(lambda x:"")

def gemm(n, m, k):
    """TVM expression for vector add"""
    A = te.placeholder((n, k), dtype="float16", name='a')
    B = te.placeholder((k, m), dtype="float16", name='b')
    K = te.reduce_axis((0, k))
    C = te.compute((n, m), lambda i, j: te.sum(A[i,K]*B[K,j], axis=[K]), name='output')
    return A, B, C

def sche_gemm(sch: tvm.tir.Schedule):
    C = sch.get_block("output")

    block_size_M, block_size_N = 128, 256
    warp_size_M, warp_size_N = 64, 128
    chunk_size = 32
    warp_size = 32
    num_warp = (block_size_M * block_size_N) // (warp_size_M * warp_size_N)

    ax_M, ax_N, ax_K = sch.get_loops(C)
    grid_M, block_M = sch.split(ax_M, factors=[None, block_size_M])
    grid_N, block_N = sch.split(ax_N, factors=[None, block_size_N])
    sch.reorder(grid_M, grid_N, block_M, block_N)
    grid = sch.fuse(grid_M, grid_N)
    sch.bind(grid, "blockIdx.x")

    grid, ax_M, ax_N, ax_K = sch.get_loops(C)
    K_outer, K_inner = sch.split(ax_K, factors=[None, chunk_size])
    warp_M, inner_M = sch.split(ax_M, factors=[None, warp_size_M])
    warp_N, inner_N = sch.split(ax_N, factors=[None, warp_size_N])
    sch.reorder(warp_M, warp_N, K_outer, inner_M, inner_N, K_inner)
    warp = sch.fuse(warp_M, warp_N)
    sch.bind(warp, "threadIdx.y")

    layoutB = RowMajorTensorOpMultiplicandCongruous(block_size_N)
    layoutA = RowMajorTensorOpMultiplicandCrosswise(chunk_size)

    for idx in [0, 1]:
        layout = layoutA if idx==0 else layoutB
        SS = sch.cache_read(C, idx, "shared")
        sch.compute_at(SS, K_outer)
        if layout.requires_padding():
            pad_size = 4 if idx == 0 else 8 # m8n8k4
            layout.set_pad(pad_size)
            sch.storage_align(SS, 0, axis=-2, factor=32, offset=pad_size)
        fused = sch.fuse(*sch.get_loops(SS)[-2:])
        vectorize_size = layout.get_vectorize()
        oo, idx_y, idx_x, vec = sch.split(fused, [None, num_warp, warp_size, vectorize_size])
        sch.bind(idx_x, "threadIdx.x")
        sch.bind(idx_y, "threadIdx.y")
        sch.vectorize(vec)
        sch.unroll(oo)

    cls_code = register_cutlass_warp_mma(warp_size_M, warp_size_N, chunk_size,
        layoutA.smem_layout_name(), layoutA.local_layout_name(),
        layoutB.smem_layout_name(), layoutB.local_layout_name())
    C_warp = sch.cache_write(C, 0, "cutlass.warp.mma")
    sch.reverse_compute_at(C_warp, warp)

    sch.decompose_reduction(C, sch.get_loops(C)[2])
    block_init_c = sch.get_block("output_init")
    layoutC = FragmentCLayout8x8(warp_size_M, warp_size_N)

    sch.blockize(sch.get_loops(C_warp)[-2])
    sch.transform_block_layout(C_warp, layoutC)
    sch.transform_layout(C_warp, ("read", 0), layoutC)
    sch.bind(sch.get_loops(C_warp)[-2], "threadIdx.x")
    oo, vec = sch.split(sch.get_loops(C_warp)[-1], factors=[None, layoutC.get_vectorize()])
    sch.vectorize(vec)
    sch.unroll(oo)
    sch.tensorize(sch.get_loops(block_init_c)[-2],
        register_cutlass_warp_init_intrin(warp_size_M, warp_size_N, "float16", layoutC.get(),
        cls_code, block_size_M // warp_size_M, block_size_N // warp_size_N)
    )
    sch.tensorize(sch.get_loops(C)[-3],
        register_gemm_intrin(
            warp_size_M, warp_size_N, chunk_size, "float16", "float16", False, False, layoutA, layoutB, layoutC)
    )
    memopt.get_scope().apply_buffer_layout["a_shared"] = layoutA
    memopt.get_scope().apply_buffer_layout["b_shared"] = layoutB
    # print(sch.mod["main"].script())
    # exit(0)

    grid = [np.prod(args[-1].shape) // block_size_M // block_size_N, 1, 1]
    block = [warp_size, num_warp, 1]
    return grid, block


args = gemm(8192, 8192, 8192)
workload = te.create_prim_func(args)
ir_module = tvm.IRModule({"main": workload})
sch = tvm.tir.Schedule(ir_module)
from memopt.IRpass import *

passes = [
    (1, apply_layout_transform_pass),
    (1, fragment_access_rewrite_pass),
]
with memopt.Scope(sch):
    grid, block = sche_gemm(sch)
    with tvm.transform.PassContext(config={"tir.add_lower_pass": passes}, disabled_pass=["tir.UnrollLoop"]):
        mod = tvm.build(sch.mod["main"], target="cuda")
kernel_code = mod.imported_modules[0].get_source()
kernel_code = kernel_code[kernel_code.index('extern "C" __global__ void'):]

print(kernel_code)
cp = CompileResult(None, kernel_code, block, grid, "default_function_kernel0", args)
cp.compile_and_load(memopt.arch.g3090())
a = cp.get_example_outputs()[0]
print(a)
print(cp.profile())

# from memopt.reference import get_reference_output

# oo = get_reference_output(args)[-1].numpy()
# print(oo)
# print(abs(oo - a).max())