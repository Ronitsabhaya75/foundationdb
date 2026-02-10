cmake_minimum_required(VERSION 3.24)
project(BoostConfigTest)

# Test if Boost can be found using CONFIG mode
find_package(Boost 1.90.0 EXACT REQUIRED 
  COMPONENTS filesystem iostreams serialization system program_options url context
  CONFIG
)

if(Boost_FOUND)
  message(STATUS "SUCCESS: Boost found using CONFIG mode!")
  message(STATUS "Boost version: ${Boost_VERSION}")
  message(STATUS "Boost include dirs: ${Boost_INCLUDE_DIRS}")
endif()
