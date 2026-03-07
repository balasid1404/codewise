"""Dynamic query-aware boosting with feedback learning."""

import re
from typing import Optional
from indexer.entities import CodeEntity


class SmartBooster:
    """
    Dynamically boosts relevance based on query analysis and learned feedback.
    No hardcoded domains - learns patterns from queries and user corrections.
    """
    
    # Universal penalties (always apply)
    UNIVERSAL_PENALTIES = [
        (r"/tst/", 0.3),
        (r"/test/", 0.3),
        (r"test\.java$", 0.3),
        (r"_test\.py$", 0.3),
        (r"tests\.py$", 0.3),
        (r"mock", 0.2),
        (r"fake", 0.2),
        (r"stub", 0.2),
        (r"dummy", 0.2),
    ]
    
    # Universal boosts (always apply)
    UNIVERSAL_BOOSTS = [
        (r"/src/", 1.2),
        (r"/main/", 1.2),
        (r"service", 1.15),
        (r"handler", 1.15),
        (r"executor", 1.15),
        (r"processor", 1.15),
        (r"manager", 1.1),
        (r"controller", 1.1),
    ]

    def __init__(self, use_feedback: bool = True):
        self.query_tokens: set[str] = set()
        self.query_bigrams: set[str] = set()
        self.feedback_boosts: dict[str, float] = {}
        self.use_feedback = use_feedback
        
        # Load feedback store if enabled
        if use_feedback:
            try:
                from feedback import FeedbackStore
                self.feedback_store = FeedbackStore()
            except ImportError:
                self.feedback_store = None
        else:
            self.feedback_store = None

    def analyze_query(self, query: str) -> None:
        """Extract meaningful tokens from query and load feedback boosts."""
        query_lower = query.lower()
        
        # Extract words (filter out common stop words)
        stop_words = {
            'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
            'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
            'could', 'should', 'may', 'might', 'must', 'shall', 'can',
            'for', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'from',
            'with', 'by', 'about', 'into', 'through', 'during', 'before',
            'after', 'above', 'below', 'between', 'under', 'again',
            'further', 'then', 'once', 'here', 'there', 'when', 'where',
            'why', 'how', 'all', 'each', 'few', 'more', 'most', 'other',
            'some', 'such', 'no', 'nor', 'not', 'only', 'own', 'same',
            'so', 'than', 'too', 'very', 'just', 'both', 'we', 'they',
            'i', 'you', 'he', 'she', 'it', 'this', 'that', 'these', 'those',
            'what', 'which', 'who', 'whom', 'whose', 'if', 'because',
            'as', 'until', 'while', 'of', 'rs', 'get', 'set', 'find'
        }
        
        words = re.findall(r'[a-z][a-z0-9_]*', query_lower)
        self.query_tokens = {w for w in words if w not in stop_words and len(w) > 2}
        
        # Extract bigrams
        word_list = [w for w in words if w not in stop_words]
        self.query_bigrams = {
            f"{word_list[i]}_{word_list[i+1]}" 
            for i in range(len(word_list) - 1)
        }
        
        # Extract camelCase/snake_case identifiers
        identifiers = re.findall(r'[A-Z][a-z]+(?:[A-Z][a-z]+)*|[a-z]+(?:_[a-z]+)+', query)
        for ident in identifiers:
            parts = re.findall(r'[A-Z]?[a-z]+', ident)
            self.query_tokens.update(p.lower() for p in parts if len(p) > 2)
        
        # Load learned boosts from feedback
        self.feedback_boosts = {}
        if self.feedback_store:
            self.feedback_boosts = self.feedback_store.get_boosts_for_query(query)

    def _tokenize_entity(self, entity: CodeEntity) -> set[str]:
        """Extract tokens from entity name and path."""
        text = f"{entity.full_name} {entity.file_path}"
        
        # Split camelCase and snake_case
        parts = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)', text)
        return {p.lower() for p in parts if len(p) > 2}

    def boost_score(self, entity: CodeEntity, base_score: float) -> float:
        """Apply dynamic boosting based on query-entity overlap and feedback."""
        score = base_score
        match_text = f"{entity.full_name} {entity.file_path}".lower()
        
        # 1. Apply universal penalties
        for pattern, multiplier in self.UNIVERSAL_PENALTIES:
            if re.search(pattern, match_text, re.IGNORECASE):
                score *= multiplier
        
        # 2. Apply universal boosts
        for pattern, multiplier in self.UNIVERSAL_BOOSTS:
            if re.search(pattern, match_text, re.IGNORECASE):
                score *= multiplier
        
        # 3. Apply learned feedback boosts (from past corrections)
        for pattern, boost in self.feedback_boosts.items():
            if pattern.lower() in match_text:
                score *= boost
        
        # 4. Dynamic boosting based on query token overlap
        entity_tokens = self._tokenize_entity(entity)
        
        direct_matches = self.query_tokens & entity_tokens
        if direct_matches:
            boost = 1 + (0.3 * len(direct_matches)) + (0.1 * min(len(direct_matches), 5))
            score *= boost
        
        # 5. Partial/substring matches
        for qt in self.query_tokens:
            if len(qt) >= 4:
                if qt in match_text and qt not in direct_matches:
                    score *= 1.15
        
        # 6. Boost if entity name directly contains query identifier
        for qt in self.query_tokens:
            if len(qt) >= 5 and qt in entity.full_name.lower():
                score *= 1.4
        
        return score

    def rerank(
        self, 
        query: str, 
        candidates: list[tuple[CodeEntity, float]]
    ) -> list[tuple[CodeEntity, float]]:
        """Rerank candidates with dynamic query-aware boosting."""
        self.analyze_query(query)
        
        boosted = [
            (entity, self.boost_score(entity, score))
            for entity, score in candidates
        ]
        
        # Sort by boosted score
        boosted.sort(key=lambda x: x[1], reverse=True)
        
        return boosted
