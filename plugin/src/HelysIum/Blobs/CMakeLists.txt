qml_module(helysium-blobs
    URI HelysIum.Blobs
    SOURCES
        blobgroup.cpp
        blobshape.cpp
        blobrect.cpp
        blobinvertedrect.cpp
        blobmaterial.cpp
    LIBRARIES
        Qt::Quick
)

qt_add_shaders(helysium-blobs "blob_shaders"
    BATCHABLE OPTIMIZED NOHLSL NOMSL
    GLSL "300es,330"
    PREFIX "/"
    FILES
        shaders/blob.frag
        shaders/blob.vert
)
