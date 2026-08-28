#include "sparse_channel_cc_impl.h"

#include "dense_channel_cuda.h"

#include <gnuradio/io_signature.h>
#include <gnuradio/sptr_magic.h>

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>
#include <vector>

namespace gr {
namespace sionna_channel {

sparse_channel_cc::sptr sparse_channel_cc::make(
    const std::vector<gr_complex>& coefficients,
    const std::vector<unsigned short>& delays,
    std::size_t samples_per_symbol)
{
    return gnuradio::get_initial_sptr(
        new sparse_channel_cc_impl(
            coefficients, delays, samples_per_symbol));
}

std::shared_ptr<const sparse_channel_cc_impl::channel_model>
sparse_channel_cc_impl::build_model(
    const std::vector<gr_complex>& coefficients,
    const std::vector<unsigned short>& delays,
    double noise_sigma)
{
    if (coefficients.size() != delays.size()) {
        throw std::invalid_argument(
            "coefficient and delay counts must match");
    }
    if (coefficients.empty() || coefficients.size() > kMaxTaps) {
        throw std::invalid_argument(
            "channel tap count must be between 1 and "
            + std::to_string(kMaxTaps));
    }
    if (!std::isfinite(noise_sigma) || noise_sigma < 0.0) {
        throw std::invalid_argument(
            "noise sigma must be finite and non-negative");
    }
    auto model = std::make_shared<channel_model>();
    model->noise_sigma = static_cast<float>(noise_sigma);
    model->taps.reserve(coefficients.size());
    for (std::size_t index = 0; index < coefficients.size(); ++index) {
        if (delays[index] > kMaxDelay) {
            throw std::invalid_argument(
                "tap delay exceeds the maximum of "
                + std::to_string(kMaxDelay) + " samples");
        }
        if (!std::isfinite(coefficients[index].real()) ||
            !std::isfinite(coefficients[index].imag())) {
            throw std::invalid_argument(
                "tap coefficients must be finite");
        }
        model->taps.push_back(tap{delays[index], coefficients[index]});
    }
    return model;
}

sparse_channel_cc_impl::sparse_channel_cc_impl(
    const std::vector<gr_complex>& coefficients,
    const std::vector<unsigned short>& delays,
    std::size_t samples_per_symbol)
    : gr::sync_block(
          "sparse_channel_cc",
          gr::io_signature::make(1, 1, sizeof(gr_complex)),
          gr::io_signature::make(1, 1, sizeof(gr_complex))),
      d_tail(kHistory, gr_complex(0.0F, 0.0F)),
      d_samples_per_symbol(samples_per_symbol),
      d_current(build_model(coefficients, delays, 0.0))
{
    if (!cuda_available()) {
        throw std::runtime_error(
            "sparse_channel_cc requires a CUDA device");
    }
}

void sparse_channel_cc_impl::set_channel(
    const std::vector<gr_complex>& coefficients,
    const std::vector<unsigned short>& delays,
    double noise_sigma)
{
    auto model = build_model(coefficients, delays, noise_sigma);
    std::lock_guard<std::mutex> lock(d_channel_mutex);
    d_current = std::move(model);
    d_update_count.fetch_add(1, std::memory_order_relaxed);
}

std::shared_ptr<const sparse_channel_cc_impl::channel_model>
sparse_channel_cc_impl::current_channel() const
{
    std::lock_guard<std::mutex> lock(d_channel_mutex);
    return d_current;
}

std::uint64_t sparse_channel_cc_impl::sample_count() const
{
    return d_sample_count.load(std::memory_order_relaxed);
}

std::uint64_t sparse_channel_cc_impl::update_count() const
{
    return d_update_count.load(std::memory_order_relaxed);
}

std::uint64_t sparse_channel_cc_impl::tap_count() const
{
    return current_channel()->taps.size();
}

double sparse_channel_cc_impl::noise_sigma() const
{
    return current_channel()->noise_sigma;
}

void sparse_channel_cc_impl::add_noise(
    gr_complex* out, std::size_t count, float sigma)
{
    // Split complex AWGN power across I and Q.
    const float scale = sigma * 0.70710678F;
    for (std::size_t j = 0; j < count; ++j) {
        out[j] += gr_complex(
            scale * d_normal(d_rng),
            scale * d_normal(d_rng));
    }
}

void sparse_channel_cc_impl::run_convolution(
    const channel_model& model,
    const gr_complex* window,
    gr_complex* out,
    std::size_t count) const
{
    const gr_complex* base = window - kHistory;
    std::vector<int> delays(model.taps.size());
    std::vector<gr_complex> coeffs(model.taps.size());
    for (std::size_t index = 0; index < model.taps.size(); ++index) {
        delays[index] = static_cast<int>(model.taps[index].delay);
        coeffs[index] = model.taps[index].coefficient;
    }
    cuda_dense_fir(
        delays.data(),
        reinterpret_cast<const float*>(coeffs.data()),
        static_cast<int>(model.taps.size()),
        reinterpret_cast<const float*>(base),
        static_cast<int>(kHistory),
        static_cast<int>(count),
        reinterpret_cast<float*>(out));
}

int sparse_channel_cc_impl::work(
    int noutput_items,
    gr_vector_const_void_star& input_items,
    gr_vector_void_star& output_items)
{
    const auto* input = static_cast<const gr_complex*>(input_items[0]);
    auto* output = static_cast<gr_complex*>(output_items[0]);
    const std::size_t count = static_cast<std::size_t>(noutput_items);
    if (count == 0) {
        return 0;
    }

    // Prefix the block with input history.
    d_ext.resize(kHistory + count);
    std::copy(d_tail.begin(), d_tail.end(), d_ext.begin());
    std::copy(input, input + count, d_ext.begin() + kHistory);

    auto absolute_sample = d_sample_count.load(std::memory_order_relaxed);
    std::size_t produced = 0;
    while (produced < count) {
        // Latch the newest CIR at symbol boundaries.
        const auto model = current_channel();
        std::size_t segment = count - produced;
        if (d_samples_per_symbol > 0) {
            const std::uint64_t offset =
                absolute_sample % d_samples_per_symbol;
            const std::uint64_t to_boundary =
                d_samples_per_symbol - offset;
            if (to_boundary < segment) {
                segment = static_cast<std::size_t>(to_boundary);
            }
        }
        run_convolution(
            *model,
            &d_ext[kHistory + produced],
            output + produced,
            segment);
        if (model->noise_sigma > 0.0F) {
            add_noise(output + produced, segment, model->noise_sigma);
        }
        produced += segment;
        absolute_sample += segment;
    }

    // Carry input history into the next block.
    std::copy(
        d_ext.end() - static_cast<std::ptrdiff_t>(kHistory),
        d_ext.end(),
        d_tail.begin());
    d_sample_count.store(absolute_sample, std::memory_order_relaxed);
    return noutput_items;
}

}
}
