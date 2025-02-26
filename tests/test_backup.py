import unittest
from unittest.mock import Mock

import ou_dedetai.backup as backup


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.app = Mock()
        self.app.conf = Mock()

    @unittest.skip("Not tested.")
    def test_copy_data(self):
        pass



class TestRestore(unittest.TestCase):
    pass