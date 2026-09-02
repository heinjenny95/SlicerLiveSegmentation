cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(SlicerLiveSegmentation)

set(EXTENSION_VERSION "0.10.3")
set(EXTENSION_HOMEPAGE "https://github.com/heinjenny95/SlicerLiveSegmentation")
set(EXTENSION_CATEGORY "Segmentation")
set(EXTENSION_CONTRIBUTORS "Jenny Hein (KIT)")
set(EXTENSION_DESCRIPTION "Live collaboration for standard 3D Slicer segmentation nodes through a shared folder or optional server.")
set(EXTENSION_ICONURL "")
set(EXTENSION_SCREENSHOTURLS "")
set(EXTENSION_DEPENDS "NA")

find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

add_subdirectory(LiveSegmentation)

include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
