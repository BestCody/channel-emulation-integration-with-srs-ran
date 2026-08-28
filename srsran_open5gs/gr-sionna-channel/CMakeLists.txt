cmake_minimum_required(VERSION 3.16)
project(gr-sionna-channel VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

enable_language(CUDA)

include(GNUInstallDirs)

find_package(Gnuradio 3.8 REQUIRED COMPONENTS runtime)
find_package(Python3 REQUIRED COMPONENTS Interpreter Development)
find_package(SWIG REQUIRED)

add_subdirectory(include)
add_subdirectory(lib)
add_subdirectory(python)
