#pragma once

#include <cstddef>

namespace gr {
namespace sionna_channel {

bool cuda_available();

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
