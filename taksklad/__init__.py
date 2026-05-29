import os


SRC_PACKAGE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "src", "taksklad")
)
if os.path.isdir(SRC_PACKAGE_DIR) and SRC_PACKAGE_DIR not in __path__:
    __path__.append(SRC_PACKAGE_DIR)
