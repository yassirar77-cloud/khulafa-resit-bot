"""db_client.ThreadLocalClient: one client per thread behind one name."""

import threading
import unittest

from db_client import ThreadLocalClient


class _Fake:
    def __init__(self):
        self.thread = threading.get_ident()

    def table(self, name):
        return (name, self.thread)


class ThreadLocalClientTest(unittest.TestCase):
    def test_same_thread_reuses_one_client(self):
        made = []
        proxy = ThreadLocalClient(lambda: made.append(_Fake()) or made[-1])
        self.assertEqual(proxy.table("a"), ("a", threading.get_ident()))
        proxy.table("b")
        self.assertEqual(len(made), 1)

    def test_each_thread_gets_its_own_client(self):
        made = []
        lock = threading.Lock()

        def factory():
            client = _Fake()
            with lock:
                made.append(client)
            return client

        proxy = ThreadLocalClient(factory)
        seen = []

        def work():
            seen.append(proxy.table("x")[1])

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        proxy.table("x")
        self.assertEqual(len(made), 5)
        self.assertEqual(len(set(seen)), 4)
        # Every call was served by a client built on the calling thread.
        self.assertEqual(sorted(seen), sorted(c.thread for c in made[:4]))

    def test_nothing_is_created_until_first_use(self):
        made = []
        ThreadLocalClient(lambda: made.append(1))
        self.assertEqual(made, [])


if __name__ == "__main__":
    unittest.main()
