#include "dense_channel_cuda.h"

#include <cuComplex.h>
#include <cuda_runtime.h>

namespace gr {
namespace sionna_channel {

namespace {

__global__ void dense_fir_kernel(
    const int* delays,
    const cuFloatComplex* coeffs,
    int ntaps,
    const cuFloatComplex* base,
    int prefix,
    int count,
    cuFloatComplex* out)
{
    const int j = blockIdx.x * blockDim.x + threadIdx.x;
    if (j >= count) {
        return;
    }
    cuFloatComplex acc = make_cuFloatComplex(0.0f, 0.0f);
    for (int t = 0; t < ntaps; ++t) {
        const int idx = prefix + j - delays[t];
        acc = cuCaddf(acc, cuCmulf(coeffs[t], base[idx]));
    }
    out[j] = acc;
}

}

bool cuda_available()
{
    int devices = 0;
    return cudaGetDeviceCount(&devices) == cudaSuccess && devices > 0;
}

void cuda_dense_fir(
    const int* delays,
    const float* coeffs,
    int ntaps,
    const float* base,
    int prefix,
    int count,
    float* out)
{
    if (count <= 0 || ntaps <= 0) {
        return;
    }
    const int base_len = prefix + count;

    int* d_delays = nullptr;
    cuFloatComplex* d_coeffs = nullptr;
    cuFloatComplex* d_base = nullptr;
    cuFloatComplex* d_out = nullptr;
    cudaMalloc(&d_delays, ntaps * sizeof(int));
    cudaMalloc(&d_coeffs, ntaps * sizeof(cuFloatComplex));
    cudaMalloc(&d_base, base_len * sizeof(cuFloatComplex));
    cudaMalloc(&d_out, count * sizeof(cuFloatComplex));

    cudaMemcpy(d_delays, delays, ntaps * sizeof(int), cudaMemcpyHostToDevice);
    cudaMemcpy(d_coeffs, coeffs, ntaps * sizeof(cuFloatComplex), cudaMemcpyHostToDevice);
    cudaMemcpy(d_base, base, base_len * sizeof(cuFloatComplex), cudaMemcpyHostToDevice);

    const int threads = 256;
    const int blocks = (count + threads - 1) / threads;
    dense_fir_kernel<<<blocks, threads>>>(
        d_delays, d_coeffs, ntaps, d_base, prefix, count, d_out);

    cudaMemcpy(out, d_out, count * sizeof(cuFloatComplex), cudaMemcpyDeviceToHost);

    cudaFree(d_delays);
    cudaFree(d_coeffs);
    cudaFree(d_base);
    cudaFree(d_out);
}

}
}
