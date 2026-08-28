add_library(
    gnuradio-sionna_channel
    SHARED
    sparse_channel_cc_impl.cc
)

target_sources(
    gnuradio-sionna_channel
    PRIVATE dense_channel_cuda.cu
)
set_target_properties(
    gnuradio-sionna_channel
    PROPERTIES CUDA_ARCHITECTURES "86"
)

target_include_directories(
    gnuradio-sionna_channel
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/../include>
        $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
)

target_link_libraries(
    gnuradio-sionna_channel
    PUBLIC
        gnuradio::gnuradio-runtime
)

target_compile_definitions(
    gnuradio-sionna_channel
    PRIVATE gnuradio_sionna_channel_EXPORTS
)

set_target_properties(
    gnuradio-sionna_channel
    PROPERTIES OUTPUT_NAME gnuradio-sionna_channel
)

install(
    TARGETS gnuradio-sionna_channel
    LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
)
