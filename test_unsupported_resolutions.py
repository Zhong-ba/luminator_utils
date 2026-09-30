import csv
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

import luminator_to_axion as converter


class UnsupportedResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database, _ = converter._extract_builtin_template(self.root / 'template')
        self.profiles = converter.DbfTemplate(
            converter._ci(self.database, 'Affichrs.DBF')).active_records()
        self.target = next(profile for profile in self.profiles
                           if profile['TYPE'] == 'M' and profile['DIMH'] > 20
                           and 'COLOR SIGN' not in profile['AFF_NAME'].upper())
        self.spec = dict(id=101, name='FRONT', width=987, height=37, is_color=False)

    def test_unknown_resolution_requires_explicit_choice(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported resolution'):
            converter.ensure_axion_displays(self.database, [self.spec])
        with self.assertRaisesRegex(ValueError, 'explicitly select resize'):
            converter.ensure_axion_displays(
                self.database, [self.spec], {101: dict(display=self.target['AFF_ID'])})

    def test_resize_uses_existing_target_profile(self):
        mapping, created, _ = converter.ensure_axion_displays(
            self.database, [self.spec],
            {101: dict(display=self.target['AFF_ID'], resize=True)})
        source = next(source for source in converter.inspect_template(self.database)
                      if source['src'] == mapping[101])
        self.assertEqual(source['aff'], self.target['AFF_ID'])
        self.assertEqual((source['w'], source['h']),
                         (self.target['DIMH'], self.target['DIMV']))
        self.assertEqual(created, [])

    def test_suggestions_do_not_invent_resolutions(self):
        sign = dict(self.spec, is_console=False)
        suggestions, profiles, _ = converter._suggest_axion_sign_mappings([sign])
        self.assertIn(101, suggestions)
        self.assertEqual({int(profile['AFF_ID']) for profile in profiles},
                         {int(profile['AFF_ID']) for profile in self.profiles})
        self.assertNotIn((987, 37), {(profile['DIMH'], profile['DIMV']) for profile in profiles})

    def test_discard_does_not_allocate_a_source(self):
        mapping, created, changed = converter.ensure_axion_displays(
            self.database, [self.spec], {101: dict(discard=True)})
        self.assertEqual((mapping, created, changed), ({}, [], []))

    def test_resolution_prompt_is_visible_and_closable_from_button(self):
        self.check_resolution_prompt('close')

    def test_create_profile_choice_from_button(self):
        self.check_resolution_prompt('create')

    def check_resolution_prompt(self, action):
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        self.addCleanup(root.destroy)
        sign = dict(self.spec, is_console=False, physical_id=1, physical_address=1)
        result = {}
        failures = []
        attempts = {'count': 0}
        chosen = {'value': False}

        def descendants(widget):
            widgets = []
            for child in widget.winfo_children():
                widgets.append(child)
                widgets.extend(descendants(child))
            return widgets

        def close_prompt():
            try:
                attempts['count'] += 1
                self.assertLess(attempts['count'], 80, 'Resolution prompt did not appear')
                prompt = next((widget for widget in descendants(root)
                               if isinstance(widget, tk.Toplevel)
                               and widget.title() == 'Resolution Not in Template'), None)
                if prompt is None:
                    if chosen['value']:
                        dialog = next(widget for widget in root.winfo_children()
                                      if isinstance(widget, tk.Toplevel))
                        tree = next(widget for widget in descendants(dialog)
                                    if isinstance(widget, ttk.Treeview))
                        self.assertIn('unsupported', tree.item('101', 'tags'))
                        next(widget for widget in descendants(dialog)
                             if isinstance(widget, ttk.Button)
                             and widget.cget('text') == 'Continue').invoke()
                        return
                    root.after(50, close_prompt)
                    return
                self.assertTrue(prompt.winfo_viewable())
                self.assertIs(prompt.grab_current(), prompt)
                if action == 'create':
                    next(widget for widget in descendants(prompt)
                         if isinstance(widget, ttk.Button)
                         and widget.cget('text') == 'Create exact-size profile').invoke()
                    chosen['value'] = True
                    root.after(50, close_prompt)
                else:
                    prompt.tk.call(prompt.protocol('WM_DELETE_WINDOW'))
            except Exception as error:
                failures.append(error)
                for widget in root.winfo_children():
                    if isinstance(widget, tk.Toplevel):
                        widget.destroy()

        def click_convert():
            result['overrides'] = converter._edit_sign_mappings(root, self.root / 'source.ips')
            root.quit()

        button = ttk.Button(root, text='Convert', command=click_convert)
        button.pack()
        root.after(0, button.invoke)
        root.after(50, close_prompt)
        with patch.object(converter, '_ips_matrix_signs', return_value=[sign]):
            root.mainloop()
        if failures:
            raise failures[0]
        if action == 'create':
            self.assertTrue(result['overrides'][101]['create_profile'])
            self.assertFalse(result['overrides'][101]['resize'])
            self.assertEqual(result['overrides'][101]['display'], '')
        else:
            self.assertIsNone(result['overrides'])
        self.assertIsNone(root.grab_current())

    def run_conversion(self, overrides, image):
        def build_export(ips, output, **kwargs):
            output.mkdir(parents=True)
            image.save(output / 'frame.png')
            row = dict(msg_class=3, msg_code=1, logical_sign_id=101,
                       logical_sign='FRONT', width=image.width, height=image.height,
                       exposure=1, relative_path='frame.png')
            with (output / 'manifest.tsv').open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(row), delimiter='\t')
                writer.writeheader()
                writer.writerow(row)
        core = SimpleNamespace(build_export=build_export, Jet3Reader=Mock(side_effect=KeyError))
        output = self.root / 'converted.zip'
        with patch.object(converter, '_load_renderer', return_value=core), \
                patch.object(converter, '_ips_special_class_a_codes', return_value={}), \
                patch.object(converter, '_ips_matrix_signs', return_value=[]):
            stats = converter.convert(self.root / 'source.ips', output, sign_overrides=overrides)
        return stats, output

    def test_resized_pic_contains_target_sized_pixels(self):
        image = Image.new('1', (987, 37), 0)
        image.paste(1, (100, 4, 500, 25))
        stats, output = self.run_conversion(
            {101: dict(display=self.target['AFF_ID'], resize=True)}, image)
        self.assertEqual(stats['mono_pictograms'], 1)
        expected = Image.new('1', (self.target['DIMH'], self.target['DIMV']), 0)
        expected.paste(image, ((expected.width - image.width) // 2,
                       (expected.height - image.height) // 2))
        with zipfile.ZipFile(output) as archive:
            pictogram = next(name for name in archive.namelist() if name.upper().endswith('.PIC'))
            self.assertEqual(archive.read(pictogram), converter.image_to_axion_pic(expected))
            report = next(name for name in archive.namelist() if name.endswith('CONVERSION_REPORT.txt'))
            self.assertIn('original pixels centered, no scaling', archive.read(report).decode('utf-8'))

    def test_larger_canvas_preserves_original_pixels(self):
        image = Image.new('1', (self.target['DIMH'] - 9, self.target['DIMV'] - 3), 0)
        image.paste(1, (1, 1, 4, 3))
        _, output = self.run_conversion(
            {101: dict(display=self.target['AFF_ID'], resize=True)}, image)
        expected = Image.new('1', (self.target['DIMH'], self.target['DIMV']), 0)
        expected.paste(image, (4, 1))
        with zipfile.ZipFile(output) as archive:
            pictogram = next(name for name in archive.namelist() if name.upper().endswith('.PIC'))
            self.assertEqual(archive.read(pictogram), converter.image_to_axion_pic(expected))

    def test_custom_profile_keeps_native_pic_pixels(self):
        image = Image.new('1', (112, 14), 0)
        image.paste(1, (5, 2, 90, 12))
        stats, output = self.run_conversion({101: dict(create_profile=True)}, image)
        self.assertEqual(len(stats['created_displays']), 1)
        self.assertEqual((stats['created_displays'][0]['DIMH'],
                          stats['created_displays'][0]['DIMV']), (112, 14))
        with zipfile.ZipFile(output) as archive:
            pictogram = next(name for name in archive.namelist() if name.upper().endswith('.PIC'))
            self.assertEqual(archive.read(pictogram), converter.image_to_axion_pic(image))
            report = next(name for name in archive.namelist() if name.endswith('CONVERSION_REPORT.txt'))
            self.assertIn('[custom profile approved]', archive.read(report).decode('utf-8'))

    def test_resized_color_bmp_uses_target_dimensions(self):
        target = next(profile for profile in self.profiles
                      if 'COLOR SIGN' in profile['AFF_NAME'].upper()
                      and profile['DIMH'] > 0 and profile['DIMV'] > 0)
        image = Image.new('RGB', (target['DIMH'] - 8, target['DIMV'] - 4), 'red')
        image.paste('green', (1, 1, 4, 3))
        expected = Image.new('RGB', (target['DIMH'], target['DIMV']), 'black')
        expected.paste(image, (4, 2))
        stats, output = self.run_conversion(
            {101: dict(display=target['AFF_ID'], resize=True)}, image)
        self.assertEqual(stats['color_pictograms'], 1)
        with zipfile.ZipFile(output) as archive:
            pictogram = next(name for name in archive.namelist()
                             if 'ColorSignPictogram' in name and name.upper().endswith('.BMP'))
            with archive.open(pictogram) as stream, Image.open(stream) as result:
                self.assertEqual(result.size, (target['DIMH'], target['DIMV']))
                self.assertEqual(result.convert('RGB').tobytes(), expected.tobytes())

    def test_skipped_sign_emits_no_messages(self):
        stats, output = self.run_conversion({101: dict(discard=True)}, Image.new('1', (987, 37)))
        self.assertEqual(stats['pictograms'], 0)
        self.assertEqual(stats['msg_rows'], 0)
        self.assertEqual(stats['codes'], 0)
        with zipfile.ZipFile(output) as archive:
            self.assertFalse(any(name.upper().endswith('.PIC') for name in archive.namelist()))


if __name__ == '__main__':
    unittest.main()