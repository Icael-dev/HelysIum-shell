qml_module(helysium-state
    URI HelysIum.State
    INCLUDE_PREFIX state
    SOURCES
        common.cpp
        rootnodes.cpp
    DEPENDENCIES
        HelysIum.Settings
)
target_link_libraries(helysium-state PUBLIC helysium-settings)
