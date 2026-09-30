import unittest
from pathlib import Path
from unittest.mock import Mock

import luminator_to_axion as converter


class SpecialCodeTests(unittest.TestCase):
    def detect(self, rows):
        database = Mock()
        database.rows.return_value = iter(rows)
        core = Mock()
        core.Jet3Reader.return_value = database
        result = converter._ips_special_class_a_codes(core, Path('fixture.ips'))
        database.find_table.assert_called_once_with(
            'MsgFrameID', 'MsgCode', 'LSignID', 'Phrase', 'FontID', 'XPos', 'YPos', 'Frame')
        return result

    def test_fixed_codes_do_not_depend_on_message_wording(self):
        result = self.detect([
            {'MsgClassID': 1, 'MsgCode': 2, 'Phrase': 'APPELEZ LA POLICE'},
            {'MsgClassID': 1, 'MsgCode': 21, 'Phrase': ''},
            {'MsgClassID': 1, 'MsgCode': 2, 'Phrase': 'SECOND FRAME'},
        ])
        self.assertEqual(result, {2: ('ZZZZ', 9998, 'ZZZZ'), 21: ('$YLD', 9997, '$YLD')})

    def test_other_classes_and_keyword_matches_are_not_special(self):
        result = self.detect([
            {'MsgClassID': 3, 'MsgCode': 2, 'Phrase': 'EMERGENCY'},
            {'MsgClassID': 2, 'MsgCode': 21, 'Phrase': 'YIELD'},
            {'MsgClassID': 1, 'MsgCode': 17, 'Phrase': 'EMERGENCY YIELD'},
            {'MsgClassID': 1, 'MsgCode': None, 'Phrase': 'EMERGENCY'},
        ])
        self.assertEqual(result, {})

    def test_absent_special_codes_are_not_created(self):
        self.assertEqual(self.detect([]), {})
        self.assertEqual(self.detect([{'MsgClassID': 1, 'MsgCode': 2}]),
                         {2: ('ZZZZ', 9998, 'ZZZZ')})


if __name__ == '__main__':
    unittest.main()