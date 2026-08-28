#pragma once

#include <gnuradio/gr_complex.h>
#include <gnuradio/sionna_channel/api.h>
#include <gnuradio/sync_block.h>

#include <boost/shared_ptr.hpp>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace gr {
    namespace sionna_channel {

        class SIONNA_CHANNEL_API sparse_channel_cc : virtual public gr::sync_block{
            public:
                using sptr = boost::shared_ptr<sparse_channel_cc>;

                static sptr make(
                    const std::vector<gr_complex>& coefficients,
                    const std::vector<unsigned short>& delays,
                    std::size_t samples_per_symbol = 0);

                // Apply CIR updates at symbol boundaries.
                virtual void set_channel(
                    const std::vector<gr_complex>& coefficients,
                    const std::vector<unsigned short>& delays,
                    double noise_sigma) = 0;

                virtual std::uint64_t sample_count() const = 0;
                virtual std::uint64_t update_count() const = 0;
                virtual std::uint64_t tap_count() const = 0;
                virtual double noise_sigma() const = 0;
        };
    }
}
