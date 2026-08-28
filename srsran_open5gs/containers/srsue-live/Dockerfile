FROM ghcr.io/sulaimanalmani/srsranzmq/srsue@sha256:7ede93eb1238df0f36afa65c3d963198beac0a4a3ec76f7b98bad2cd893614a4 AS builder

USER root

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        liborc-0.4-dev \
        swig \
        wget \
        ca-certificates \
        gnupg \
    && rm -rf /var/lib/apt/lists/*

RUN wget -qO /tmp/cuda-keyring.deb \
        https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/x86_64/cuda-keyring_1.1-1_all.deb \
    && dpkg -i /tmp/cuda-keyring.deb \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        cuda-nvcc-12-4 \
        cuda-cudart-dev-12-4 \
    && rm -rf /var/lib/apt/lists/* /tmp/cuda-keyring.deb
ENV PATH=/usr/local/cuda/bin:${PATH}

RUN test "$(gnuradio-config-info --version)" = "3.8.1.0" \
    && test "$(pkg-config --modversion gnuradio-runtime)" = "3.8.1"

COPY gr-sionna-channel /tmp/gr-sionna-channel

RUN cmake \
        -S /tmp/gr-sionna-channel \
        -B /tmp/gr-sionna-channel/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build /tmp/gr-sionna-channel/build --parallel \
    && cmake --install /tmp/gr-sionna-channel/build \
    && ldconfig

FROM ghcr.io/sulaimanalmani/srsranzmq/srsue@sha256:7ede93eb1238df0f36afa65c3d963198beac0a4a3ec76f7b98bad2cd893614a4

USER root

COPY --from=builder /usr/local/ /usr/local/

RUN echo "/usr/local/cuda/lib64" > /etc/ld.so.conf.d/cuda.conf \
    && ldconfig \
    && python3 -c \
        "from sionna_channel import sparse_channel_cc; print(sparse_channel_cc)"
