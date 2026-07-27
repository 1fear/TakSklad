"""Browser print contract for the operator web surface.

Two invariants protect the warehouse operator:

1. The print stylesheet may only blank the page while the print sheet modal is
   actually open. A global `body *{ visibility: hidden }` would silently turn
   every ordinary browser print (Ctrl+P) on `/` into an empty sheet.
2. `window.print()` stays reachable only from the explicit print button, so no
   code path can start a silent print.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = ROOT / "frontend/src"
PRINT_SCOPE_CLASS = "warehouse-print-open"


def media_print_blocks(css: str):
    blocks = []
    for match in re.finditer(r"@media\s+print\s*\{", css):
        depth = 1
        index = match.end()
        while index < len(css) and depth:
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
            index += 1
        blocks.append(css[match.end():index - 1])
    return blocks


def declaration_rules(block: str):
    return re.findall(r"([^{}]+)\{([^{}]*)\}", block)


class FrontendPrintContractTests(unittest.TestCase):
    def stylesheets(self):
        return sorted(FRONTEND_SRC.rglob("*.css"))

    def test_print_stylesheet_blanks_the_page_only_while_the_modal_is_open(self):
        checked_blocks = 0
        for path in self.stylesheets():
            for block in media_print_blocks(path.read_text(encoding="utf-8")):
                checked_blocks += 1
                for selector, body in declaration_rules(block):
                    if "visibility" not in body or "hidden" not in body:
                        continue
                    for single in selector.split(","):
                        single = single.strip()
                        if not single:
                            continue
                        self.assertIn(
                            PRINT_SCOPE_CLASS,
                            single,
                            f"{path.relative_to(ROOT)}: print rule '{single}' hides content "
                            f"outside .{PRINT_SCOPE_CLASS}; ordinary Ctrl+P would print a blank sheet",
                        )

        self.assertGreaterEqual(checked_blocks, 1, "expected at least one @media print block")

    def test_print_sheet_stays_visible_while_the_modal_is_open(self):
        css = (FRONTEND_SRC / "features/warehouse/WarehousePanel.css").read_text(encoding="utf-8")
        blocks = media_print_blocks(css)
        self.assertEqual(len(blocks), 1)
        block = blocks[0]

        self.assertIn(f"body.{PRINT_SCOPE_CLASS} .warehouse-print-modal", block)
        self.assertIn("visibility: visible !important", block)

    def test_window_print_is_reachable_only_from_the_explicit_button(self):
        call_sites = []
        for path in sorted(FRONTEND_SRC.rglob("*.ts*")):
            if "__tests__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            for line_number, line in enumerate(source.splitlines(), start=1):
                if "window.print(" in line:
                    call_sites.append((path.relative_to(ROOT).as_posix(), line_number, line.strip()))

        self.assertEqual(
            [site[0] for site in call_sites],
            ["frontend/src/features/warehouse/PrintSheetModal.tsx"],
            f"window.print() must stay in the print modal only, found: {call_sites}",
        )
        self.assertEqual(len(call_sites), 1)
        self.assertIn("onClick", call_sites[0][2])

    def test_print_modal_does_not_auto_print_on_open(self):
        source = (FRONTEND_SRC / "features/warehouse/PrintSheetModal.tsx").read_text(encoding="utf-8")
        effect_bodies = re.findall(r"useEffect\(\(\)\s*=>\s*\{(.*?)\n  \}, \[", source, flags=re.S)
        self.assertGreaterEqual(len(effect_bodies), 1)
        for body in effect_bodies:
            self.assertNotIn("window.print(", body, "print modal must never print from an effect")


if __name__ == "__main__":
    unittest.main()
