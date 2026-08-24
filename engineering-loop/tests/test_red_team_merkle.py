from __future__ import annotations

import hashlib
import unittest

import red_team_merkle as subject


def _leaf(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class MerkleRootTests(unittest.TestCase):
    def test_empty_list_raises(self):
        with self.assertRaises(ValueError):
            subject.merkle_root([])

    def test_single_leaf_root_is_the_leaf_itself(self):
        leaf = _leaf("only-leaf")
        self.assertEqual(subject.merkle_root([leaf]), leaf)

    def test_even_count_combines_pairwise(self):
        leaves = [_leaf("a"), _leaf("b"), _leaf("c"), _leaf("d")]
        expected = subject._combine(
            subject._combine(leaves[0], leaves[1]),
            subject._combine(leaves[2], leaves[3]),
        )
        self.assertEqual(subject.merkle_root(leaves), expected)

    def test_odd_count_duplicates_last_leaf_to_pad(self):
        leaves = [_leaf("a"), _leaf("b"), _leaf("c")]
        # Nivel 1 (3 hojas, impar): se duplica la última -> [a, b, c, c]
        level1 = [
            subject._combine(leaves[0], leaves[1]),
            subject._combine(leaves[2], leaves[2]),
        ]
        expected = subject._combine(level1[0], level1[1])
        self.assertEqual(subject.merkle_root(leaves), expected)

    def test_different_orderings_produce_different_roots(self):
        leaves = [_leaf("a"), _leaf("b"), _leaf("c"), _leaf("d")]
        reordered = [_leaf("b"), _leaf("a"), _leaf("c"), _leaf("d")]
        self.assertNotEqual(subject.merkle_root(leaves), subject.merkle_root(reordered))


class VerifyMerkleTests(unittest.TestCase):
    def setUp(self):
        self.leaves = [_leaf("a"), _leaf("b"), _leaf("c"), _leaf("d"), _leaf("e")]
        self.root = subject.merkle_root(self.leaves)

    def test_match_returns_true_none(self):
        ok, index = subject.verify_merkle(self.leaves, self.root)
        self.assertTrue(ok)
        self.assertIsNone(index)

    def test_tamper_without_expected_leaves_detects_but_cannot_localize(self):
        tampered = list(self.leaves)
        tampered[2] = _leaf("tampered")
        ok, index = subject.verify_merkle(tampered, self.root)
        self.assertFalse(ok)
        self.assertIsNone(index)  # sin referencia, no se puede localizar -- ver docstring

    def test_tamper_at_first_leaf_is_localized_against_expected(self):
        tampered = list(self.leaves)
        tampered[0] = _leaf("tampered-first")
        ok, index = subject.verify_merkle(tampered, self.root, expected_leaf_hashes=self.leaves)
        self.assertFalse(ok)
        self.assertEqual(index, 0)

    def test_tamper_at_last_leaf_is_localized_against_expected(self):
        tampered = list(self.leaves)
        tampered[-1] = _leaf("tampered-last")
        ok, index = subject.verify_merkle(tampered, self.root, expected_leaf_hashes=self.leaves)
        self.assertFalse(ok)
        self.assertEqual(index, len(self.leaves) - 1)

    def test_tamper_at_middle_leaf_is_localized_against_expected(self):
        tampered = list(self.leaves)
        middle = len(self.leaves) // 2
        tampered[middle] = _leaf("tampered-middle")
        ok, index = subject.verify_merkle(tampered, self.root, expected_leaf_hashes=self.leaves)
        self.assertFalse(ok)
        self.assertEqual(index, middle)


if __name__ == "__main__":
    unittest.main()
