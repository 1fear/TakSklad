import unittest
from pathlib import Path

from taksklad.product_images import (
    PRODUCT_IMAGE_ASSETS,
    product_image_gtin,
    product_image_path,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_IMAGE_DIR = PROJECT_ROOT / "assets" / "product_images"


class ProductImagesTests(unittest.TestCase):
    def test_supported_chapman_sku_have_product_images_and_gtin(self):
        expected = {
            "Chapman Brown OP 20": "4006396053978",
            "Chapman Brown SSL 100`20": "4006396054067",
            "Chapman Gold SSL 100`20": "4006396054005",
            "Chapman Green OP 20": "4006396104441",
            "Chapman RED OP 20": "4006396053947",
            "Chapman RED SSL 100 20": "4006396054036",
        }

        for product, gtin in expected.items():
            with self.subTest(product=product):
                self.assertEqual(product_image_gtin(product), gtin)
                path = product_image_path(product)
                self.assertTrue(path, product)
                self.assertTrue(path.endswith(".png"), path)
                self.assertIn("assets/product_images", path.replace("\\", "/"))

    def test_product_images_do_not_depend_on_generated_work_folder(self):
        for asset in PRODUCT_IMAGE_ASSETS.values():
            self.assertNotIn("generated", asset["filename"])

    def test_configured_product_image_assets_exist_on_disk(self):
        filenames = {asset["filename"] for asset in PRODUCT_IMAGE_ASSETS.values()}

        self.assertEqual(len(filenames), 6)
        for filename in filenames:
            with self.subTest(filename=filename):
                path = PRODUCT_IMAGE_DIR / filename
                self.assertTrue(path.exists(), filename)
                self.assertGreater(path.stat().st_size, 1024, filename)


if __name__ == "__main__":
    unittest.main()
