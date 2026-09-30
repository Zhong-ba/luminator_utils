import tempfile
import unittest
from pathlib import Path

import luminator_to_axion as converter


class ConsoleSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database, _ = converter._extract_builtin_template(Path(self.temp.name))
        self.signs = [dict(id=101, name='ODK', width=100, height=14)]
        self.path = converter._ci(self.database, 'Configs.DBF')
        self.template = converter.DbfTemplate(self.path)
        self.console = next(source for source in converter.inspect_template(self.database)
                            if source['location'] == 'Control Console')

    def test_source_is_resolved_not_hardcoded(self):
        rows = self.template.active_records()
        for row in rows:
            if int(row['CFG_ID']) == self.console['cfg']:
                row['SRC_ID'] = 17
        self.template.write_records(self.path, rows, production_mdx=True)
        self.assertEqual(converter._ensure_console_sources(self.database, self.signs), {101: 17})
        with self.assertRaises(ValueError):
            converter.ensure_axion_displays(
                self.database, [dict(id=102, name='Front', width=160, height=16, is_color=False)],
                {102: dict(source='17')})

    def test_override_and_discard(self):
        self.assertEqual(converter._ensure_console_sources(
            self.database, self.signs, {101: dict(source='19')}), {101: 19})
        self.assertEqual(converter._ensure_console_sources(
            self.database, self.signs, {101: dict(discard=True)}), {})

    def test_zero_console_source(self):
        self.assertEqual(converter._ensure_console_sources(
            self.database, self.signs, {101: dict(source=0)}), {101: 0})
        self.assertEqual(converter._ensure_console_sources(
            self.database, self.signs, {101: dict(source='0')}), {101: 0})

    def test_remapped_console_releases_original_source_for_matrix(self):
        original_source=self.console['src']
        self.assertEqual(converter._ensure_console_sources(
            self.database, self.signs, {101: dict(source=0)}), {101: 0})
        signs=[dict(id=102, name='Front', width=160, height=16, is_color=False)]
        mapping, _, _=converter.ensure_axion_displays(
            self.database, signs, {102: dict(source=original_source)})
        self.assertEqual(mapping, {102: original_source})
        sources={source['src']:source for source in converter.inspect_template(self.database)}
        self.assertEqual(sources[0]['location'], 'Control Console')
        self.assertEqual(sources[original_source]['location'], 'Front')
        self.assertEqual(sources[original_source]['type'], 'M')
        with self.assertRaisesRegex(ValueError, 'assigned to more than one'):
            converter.ensure_axion_displays(self.database, signs, {102: dict(source=0)})

    def test_configuration_ids_follow_remapped_sources(self):
        converter._ensure_console_sources(self.database,self.signs,{101:dict(source=0)})
        converter.ensure_axion_displays(
            self.database,[dict(id=102,name='Auxiliary',width=200,height=24,is_color=False)],
            {102:dict(source=4,location='Auxiliary 1')})
        console=next(source for source in converter.inspect_template(self.database)
                     if source['src']==0)
        association=converter.DbfTemplate(converter._ci(self.database,'Assclien.DBF'))
        association.write_records(association.path,[dict(NOTABLE=1,CFG_ID=console['cfg'])])
        converter._align_config_source_ids(self.database)
        sources={source['src']:source for source in converter.inspect_template(self.database)}
        self.assertEqual(sources[0]['cfg'],0)
        self.assertEqual(sources[4]['cfg'],4)
        self.assertEqual((sources[4]['w'],sources[4]['h']),(200,24))
        links=converter.DbfTemplate(association.path).active_records()
        self.assertEqual(links[0]['CFG_ID'],0)
        for stem in ('Configs','Assclien'):
            converter.rebuild_production_mdx(
                converter._ci(self.database,stem+'.DBF'),converter._ci(self.database,stem+'.MDX'))

    def test_zero_matrix_source_is_reused(self):
        signs = [dict(id=102, name='Front', width=160, height=16, is_color=False)]
        for source in (0, '0'):
            mapping, _, _ = converter.ensure_axion_displays(
                self.database, signs, {102: dict(source=source)})
            self.assertEqual(mapping, {102: 0})
        rows = converter.DbfTemplate(self.path).active_records()
        self.assertEqual(sum(row['SRC_ID'] == 0 for row in rows), 1)

    def test_negative_source_is_rejected(self):
        with self.assertRaises(RuntimeError):
            converter._ensure_console_sources(self.database, self.signs, {101: dict(source=-1)})
        with self.assertRaises(ValueError):
            converter.ensure_axion_displays(
                self.database, [dict(id=102, name='Front', width=160, height=16, is_color=False)],
                {102: dict(source=-1)})

    def test_missing_or_ambiguous_configuration_requires_resolution(self):
        rows = self.template.active_records()
        original = next(row for row in rows if int(row['CFG_ID']) == self.console['cfg'])
        rows.append(dict(original, CFG_ID=99, SRC_ID=19))
        self.template.write_records(self.path, rows, production_mdx=True)
        with self.assertRaises(RuntimeError):
            converter._ensure_console_sources(self.database, self.signs)
        self.assertEqual(converter._ensure_console_sources(
            self.database, self.signs, {101: dict(source='19')}), {101: 19})
        rows = [row for row in rows if int(row['EMPL_ID']) != self.console['empl']]
        self.template.write_records(self.path, rows, production_mdx=True)
        with self.assertRaises(RuntimeError):
            converter._ensure_console_sources(self.database, self.signs)

    def test_separate_consoles_require_distinct_sources(self):
        signs = self.signs + [dict(id=102, name='ODK2', width=100, height=14)]
        with self.assertRaises(RuntimeError):
            converter._ensure_console_sources(self.database, signs)
        mapping = converter._ensure_console_sources(
            self.database, signs, {102: dict(source='19')})
        self.assertEqual(mapping, {101: self.console['src'], 102: 19})


if __name__ == '__main__':
    unittest.main()