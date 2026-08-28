#pragma once

#include <cstddef>

namespace gr {
namespace sionna_channel {

// Report whether CUDA is available.
bool cuda_available();

// Use interleaved complex arrays.
void cuda_dense_fir(
    const int* delays,
    const float* coeffs,
    int ntaps,
    const float* base,
    int prefix,
    int count,
    float* out);

}
}
