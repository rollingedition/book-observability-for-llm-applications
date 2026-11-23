"""BM25 retrieval over the knowledge base (plan section 2.4, step 2; probes C-5 and B-41).

bm25s returns equal scores in an order it does not document (C-5: the ID 59 came before the duplicate 58). So this
module ranks every article itself: by score, highest first, then by article ID. An article that shares no term with
the query scores zero and is never returned, so a query with no match retrieves nothing.
"""
from __future__ import annotations

import bm25s

from lib.case import Article


class Retriever:
    """BM25 over the articles' titles and bodies, returning the top `k` article IDs for a query."""

    def __init__(self, articles: list[Article], k: int):
        if k < 1:
            raise ValueError("k must be at least 1")
        ordered = sorted(articles, key=lambda a: a.article_id)
        self.k = k
        self._ids = [a.article_id for a in ordered]
        corpus = [f"{a.title}\n{a.text}" for a in ordered]
        self._index = bm25s.BM25()
        self._index.index(bm25s.tokenize(corpus, show_progress=False), show_progress=False)

    def search(self, query: str) -> list[str]:
        """The IDs of the best `k` articles for `query`: score descending, then article ID ascending."""
        tokens = bm25s.tokenize([query], show_progress=False, return_ids=False)
        if not tokens or not tokens[0]:
            return []
        docs, scores = self._index.retrieve(tokens, k=len(self._ids), show_progress=False)
        ranked = sorted(
            (-float(score), self._ids[int(doc)])
            for doc, score in zip(docs[0], scores[0])
            if float(score) > 0.0
        )
        return [article_id for _, article_id in ranked[: self.k]]
