cmake_minimum_required(VERSION 3.10)
project(AQM_Messaging)

set(CMAKE_CXX_STANDARD 17)

# 1. Include Directories
include_directories(include)
include_directories(src/common)
include_directories(src/crypto)
include_directories(src/logic_modules)
include_directories(/usr/local/include) # Standard install path for OQS

# 2. Find System Libraries
find_package(OpenSSL REQUIRED)
find_library(LIBOQS oqs HINTS /usr/local/lib)
find_library(LIBSODIUM sodium)

# Check if libraries were found
if (NOT LIBOQS)
    message(FATAL_ERROR "liboqs not found! Make sure it is installed in /usr/local/lib")
endif()

# 3. Build the Crypto Engine
add_library(crypto_lib src/crypto/crypto_engine.cpp)

# 4. Build the Server
add_executable(aqm_server src/server_module/server_main.cpp)
target_link_libraries(aqm_server 
    crypto_lib 
    ${LIBOQS} 
    OpenSSL::SSL    # <--- ADDED THIS
    OpenSSL::Crypto # <--- ADDED THIS
    ${LIBSODIUM} 
    pthread 
    dl
)

# 5. Build the Client
add_executable(aqm_client src/client_module/client_main.cpp)
target_link_libraries(aqm_client 
    crypto_lib 
    ${LIBOQS} 
    OpenSSL::SSL    # <--- ADDED THIS
    OpenSSL::Crypto # <--- ADDED THIS
    ${LIBSODIUM} 
    pthread 
    dl
)
