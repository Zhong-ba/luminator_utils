import tempfile
import unittest
from pathlib import Path

import luminator_to_axion as converter


class AxionNameTests(unittest.TestCase):
    def test_filename_default(self):
        self.assertEqual(converter._default_axion_name(Path('Example Fleet.ips')), 'Example Fleet')
        self.assertEqual(len(converter._default_axion_name(Path('A' * 30 + '.ips'))), 20)

    def test_invalid_names(self):
        for name in (' ', 'A' * 21, 'Fleet\nName', '\u4e2d'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                converter._validate_axion_name(name)
        self.assertEqual(converter._validate_axion_name(' Fleet '), 'Fleet')

    def test_template_cleanup_and_names(self):
        with tempfile.TemporaryDirectory() as temp:
            database, _ = converter._extract_builtin_template(Path(temp))
            converter._configure_axion_names(database, 'Transit Network', 'Fleet System')
            tables = {}
            for stem in ('Systemes', 'Reseaux', 'Garage'):
                dbf = converter._ci(database, stem + '.DBF')
                tables[stem] = converter.DbfTemplate(dbf).active_records()
                converter.rebuild_production_mdx(dbf, converter._ci(database, stem + '.MDX'))
            self.assertEqual(tables['Systemes'], [dict(SYS_ID=1, SYS_NOM='Fleet System', MIXTE=True)])
            self.assertEqual(tables['Reseaux'][0]['RES_NOM'], 'Transit Network')
            self.assertTrue(all(row['SYS_ID'] != 2 for row in tables['Garage']))
            self.assertTrue(any(row['SYS_ID'] == 1 for row in tables['Garage']))


if __name__ == '__main__':
    unittest.main()