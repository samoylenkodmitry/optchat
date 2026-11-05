"""Word search over the original messages.

The ranking is BM25 over whole words. A rare word weighs more than a common
word, and a long message gets no extra score for its length. A query word of
four or more letters also matches the longer words that start with it, at half
weight, so "fork" finds "forks". An identifier of the query, such as acc.txt or
a branch name, adds a bonus when the message holds it as written. Words of the
user weigh most, then replies of agents. Newer messages weigh up to twice as
much as the oldest one.

Before this, search counted the query words that a message held anywhere in
its text. "ms" matched "programs", and one pasted message of 35,000 characters
ranked in the top 3 for one query in seven.
"""
from __future__ import annotations

import bisect
import math
import re
from array import array
from collections import Counter

from .activity import stored_kind

WORD = re.compile(r"\w+")
K1, B = 1.2, 0.75
KIND_WEIGHT = {"user": 1.3, "talk": 1.15}
PREFIX = 4  # Query words with this many letters or more also match longer words that start with them.
PREFIX_WEIGHT = 0.5
EXACT = 8.0  # For each identifier of the query that the message holds as written.
PHRASE = 10.0  # When the message holds the whole query as written.
IDENTIFIER = re.compile(r"\w[^\w\s]+\w")  # A word with inner punctuation: acc.txt, ab.py, claude/first-frame.


def terms(query):
    return sorted({t for t in WORD.findall(query.casefold()) if len(t) > 1})


def identifiers(query):
    found = (w.strip(".,;:()[]{}'\"`") for w in query.casefold().split())
    return sorted({w for w in found if len(w) > 3 and IDENTIFIER.search(w)})


class Index:
    """An inverted index of the messages, updated before each search."""

    def __init__(self, store):
        self.store = store
        self.postings: dict[str, array] = {}  # word -> ids of the messages that hold it
        self.counts: dict[str, array] = {}  # word -> how often each of those messages holds it
        self.vocabulary: list[str] = []  # sorted, for prefix matches
        self.lengths = array("I")
        self.weights = array("f")  # 0 for a stored notice, which search skips
        self.kinds: list[str | None] = []
        self.words = 0
        self.searchable = 0

    def sync(self):
        root = self.store.root
        start = len(self.lengths)
        if start == len(root):
            return
        new = []
        for m in root[start:]:
            kind = stored_kind(m.kind, m.text)
            tokens = WORD.findall(m.text.casefold())
            self.kinds.append(kind)
            self.lengths.append(len(tokens))
            self.weights.append(KIND_WEIGHT.get(kind, 1.0) if kind else 0.0)
            if not kind:
                continue  # Search skips a stored notice, so its words stay out of the counts too.
            self.words += len(tokens)
            self.searchable += 1
            for word, count in Counter(tokens).items():
                ids = self.postings.get(word)
                if ids is None:
                    ids = self.postings[word] = array("I")
                    self.counts[word] = array("H")
                    new.append(word)
                ids.append(m.i)
                self.counts[word].append(min(count, 65535))
        if start == 0:
            self.vocabulary = sorted(self.postings)
        else:
            for word in new:
                bisect.insort(self.vocabulary, word)

    def forms(self, term):
        """The indexed words that a query word matches, with their weight."""
        found = [(term, 1.0)] if term in self.postings else []
        if len(term) >= PREFIX:
            at = bisect.bisect_right(self.vocabulary, term)
            while at < len(self.vocabulary) and self.vocabulary[at].startswith(term):
                found.append((self.vocabulary[at], PREFIX_WEIGHT))
                at += 1
        return found

    def search(self, query, limit):
        """[(id, word)] of the best messages, best first. The word is the rarest
        matched word or identifier of the query in that message."""
        self.sync()
        words = terms(query)
        if not words:
            raise ValueError("The query has no words.")
        n = max(1, self.searchable)
        average = self.words / n or 1.0
        scores: dict[int, float] = {}
        best: dict[int, tuple[float, str]] = {}
        holders = []  # per query word: the messages that hold it in some form
        for term in words:
            gained: dict[int, float] = {}
            spot: dict[int, str] = {}
            for word, weight in self.forms(term):
                ids, counts = self.postings[word], self.counts[word]
                idf = math.log(1 + (n - len(ids) + .5) / (len(ids) + .5))
                for i, f in zip(ids, counts):
                    if not self.weights[i]:
                        continue
                    gain = weight * idf * f * (K1 + 1) / (f + K1 * (1 - B + B * self.lengths[i] / average))
                    if gain > gained.get(i, 0.0):
                        gained[i], spot[i] = gain, word
            for i, gain in gained.items():
                scores[i] = scores.get(i, 0.0) + gain
                if gain > best.get(i, (0.0, ""))[0]:
                    best[i] = (gain, spot[i])
            holders.append(set(gained))
        root = self.store.root
        text = lambda i: root[i].text.casefold()
        for word in identifiers(query):
            parts = [holder for term, holder in zip(words, holders) if term in WORD.findall(word)]
            for i in set.intersection(*parts) if parts else ():
                if word in text(i):
                    scores[i] += EXACT
                    best[i] = (math.inf, word)
        phrase = query.casefold().strip()
        if len(words) > 1 and holders:
            for i in set.intersection(*holders):
                if phrase in text(i):
                    scores[i] += PHRASE
        total = len(root) or 1
        ranked = sorted(scores, key=lambda i: (-scores[i] * self.weights[i] * (1 + i / total), -i))
        return [(i, best[i][1]) for i in ranked[:limit]]
