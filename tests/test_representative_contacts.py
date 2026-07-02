import unittest

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, RepresentativeContact
from backend.app.representative_contacts import (
    build_representative_comment,
    display_representative_name,
    find_representative_contact,
    import_representative_contacts_from_xlsx,
    normalize_phone,
    normalize_representative_name,
)


class RepresentativeContactsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_imports_representative_contacts_from_xlsx_and_matches_aliases(self):
        path = self._workbook_path()

        with self.SessionLocal() as db:
            summary = import_representative_contacts_from_xlsx(db, path)
            db.commit()
            contact = find_representative_contact(db, "ТП1")
            by_name = find_representative_contact(db, "Умид")
            by_smartup_full_name = find_representative_contact(db, "Кобилов Достон Рустам угли")

        self.assertEqual(summary["created"], 2)
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(contact.name, "ТП-1 Умид")
        self.assertEqual(contact.work_phone, "+998 91 111 11 11")
        self.assertEqual(contact.personal_phone, "+998 90 222 22 22")
        self.assertEqual(contact.work_zone, "Юнусабад")
        self.assertEqual(by_name.id, contact.id)
        self.assertEqual(by_smartup_full_name.name, "ТП-2 Достон")

    def test_comment_keeps_payment_first_and_adds_representative_contacts(self):
        contact = RepresentativeContact(
            name="ТП-1 Умид",
            normalized_name=normalize_representative_name("ТП-1 Умид"),
            work_phone="+998 91 111 11 11",
            personal_phone="+998 90 222 22 22",
            work_zone="Юнусабад",
        )

        comment = build_representative_comment("Терминал", "ТП-1 Умид", contact)

        self.assertEqual(
            comment,
            "Терминал\n"
            "ТП1 Умид\n"
            "Раб зона: Юнусабад\n"
            "Рабочий номер: +998 91 111 11 11\n"
            "Личный номер: +998 90 222 22 22",
        )

    def test_comment_uses_tp_code_with_smartup_full_name(self):
        contact = RepresentativeContact(
            name="ТП-3 Бекзод",
            normalized_name=normalize_representative_name("ТП-3 Бекзод"),
            work_phone="+998 77 744 48 40",
            personal_phone="+998 90 000 61 61",
            work_zone="Мирзо Улугбек",
        )

        comment = build_representative_comment("Терминал", "Мирзаев Бекзод Мусажон угли", contact)

        self.assertEqual(
            comment,
            "Терминал\n"
            "ТП3 Мирзаев Бекзод Мусажон угли\n"
            "Раб зона: Мирзо Улугбек\n"
            "Рабочий номер: +998 77 744 48 40\n"
            "Личный номер: +998 90 000 61 61",
        )

    def test_comment_without_contact_still_includes_representative(self):
        self.assertEqual(build_representative_comment("Перечисление", "ТП2"), "Перечисление\nТП2")

    def test_display_representative_name_keeps_canonical_contact_for_short_smartup_name(self):
        contact = RepresentativeContact(
            name="ТП-8 Муроджон",
            normalized_name=normalize_representative_name("ТП-8 Муроджон"),
        )

        self.assertEqual(display_representative_name("Мурод", contact), "ТП8 Муроджон")

    def test_normalize_phone_formats_uzbek_numbers(self):
        self.assertEqual(normalize_phone("998931234567"), "+998 93 123 45 67")
        self.assertEqual(normalize_phone("901112233"), "+998 90 111 22 33")

    def test_representative_number_spellings_share_normalized_key(self):
        self.assertEqual(normalize_representative_name("ТП1 Умид"), "тп-1 умид")
        self.assertEqual(normalize_representative_name("ТП-1 Умид"), "тп-1 умид")

    def _workbook_path(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Лист1"
        sheet.append(["ТП", "Раб номер", "Лич номер", "Раб зона"])
        sheet.append(["ТП-1 Умид", "998911111111", "998902222222", "Юнусабад"])
        sheet.append(["ТП-2 Достон", "+998 93 333 33 33", "", "Алмазар"])
        path = "/tmp/taksklad_test_representative_contacts.xlsx"
        workbook.save(path)
        return path


if __name__ == "__main__":
    unittest.main()
