import unittest

from progress_utils import is_message_not_modified_error


class ProgressTests(unittest.TestCase):
    def test_message_not_modified(self) -> None:
        self.assertTrue(is_message_not_modified_error(Exception("Bad Request: message is not modified")))
        self.assertFalse(is_message_not_modified_error(Exception("other error")))


if __name__ == "__main__":
    unittest.main()
