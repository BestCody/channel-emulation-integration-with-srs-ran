#pragma once

#include <gnuradio/sionna_channel/sparse_channel_cc.h>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <random>
#include <vector>

namespace gr {
    namespace sionna_channel {

        class sparse_channel_cc_impl final : public sparse_channel_cc{
            public:
                sparse_channel_cc_impl(
                    const std::vector<gr_complex>& coefficients,
                    const std::vector<unsigned short>& delays,
                    std::size_t samples_per_symbol);

                void set_channel(
                    const std::vector<gr_complex>& coefficients,
                    const std::vector<unsigned short>& delays,
                    double noise_sigma) override;

                std::uint64_t sample_count() const override;
                std::uint64_t update_count() const override;
                std::uint64_t tap_count() const override;
                double noise_sigma() const override;

                int work(
                    int noutput_items,
                    gr_vector_const_void_star& input_items,
                    gr_vector_void_star& output_items) override;

            private:
                static constexpr std::size_t kMaxTaps = 1024;
                static constexpr std::size_t kMaxDelay = 1023;
                static constexpr std::size_t kHistory = kMaxDelay;

                struct tap {
                    std::uint16_t delay;
                    gr_complex coefficient;
                };

                struct channel_model {
                    std::vector<tap> taps;
                    float noise_sigma = 0.0F;
                };

                static std::shared_ptr<const channel_model> build_model(
                    const std::vector<gr_complex>& coefficients,
                    const std::vector<unsigned short>& delays,
                    double noise_sigma);

                std::shared_ptr<const channel_model> current_channel() const;
                void run_convolution(
                    const channel_model& model,
                    const gr_complex* window,
                    gr_complex* out,
                    std::size_t count) const;
                void add_noise(gr_complex* out, std::size_t count, float sigma);

                std::vector<gr_complex> d_tail;
                std::vector<gr_complex> d_ext;
                std::size_t d_samples_per_symbol;
                std::shared_ptr<const channel_model> d_current;
                mutable std::mutex d_channel_mutex;
                std::mt19937 d_rng{std::random_device{}()};
                std::normal_distribution<float> d_normal{0.0F, 1.0F};
                std::atomic<std::uint64_t> d_sample_count{0};
                std::atomic<std::uint64_t> d_update_count{0};
        };
    }
}
