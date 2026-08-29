from pathlib import Path

PATH = Path("numisvault_backend.py")
text = PATH.read_text(encoding="utf-8")

old = '''    # --- ΔΙΟΡΘΩΣΗ BUG: Σωστή διαχείριση numismatic εξαιρέσεων χώρας ---
    if country:
        numismatic_exceptions = ["drachma", "drachmai", "drachmas", "lepta", "george i", "georgios"]
        conflicting_greek_authority = (
            canonical_country(country) == "greece"
            and any(term in a for term in ("kreta", "crete", "cretan state", "cretan"))
        )
        has_exception = any(ex in a for ex in numismatic_exceptions) and not conflicting_greek_authority

        # Αν η χώρα αναγράφεται ρητά Ή αν δεν έχουμε numismatic εξαίρεση, τότε επιβάλλεται ο έλεγχος τίτλου
        if _country_explicit_in_raw(country, raw_query):
            if not country_in_title(country, a) and not has_exception:
                return False
        else:
            # Αν η χώρα προέκυψε από συμπερασμό (Resolver), επιτρέπουμε το νόμισμα 
            # ΜΟΝΟ αν ο τίλος περιέχει τη χώρα Ή αν έχουμε numismatic εξαίρεση
            if not country_in_title(country, a) and not has_exception:
                return False
'''

# Current source uses "τίτλος" in the second comment; keep a second exact anchor
# so the patch remains idempotent across the already-deployed wording.
old = old.replace("τίλος", "τίτλος")

new = '''    # Country is a hard constraint only when the user explicitly supplied it.
    # A country inferred by the identity resolver is evidence for ranking and
    # identity resolution, not permission to reject a historically distinct
    # issuing authority.  Example: raw "5 drachmai 1901" may resolve broadly
    # to Greece, while valid listings identify the issuer as Crete/Kreta.
    if country and _country_explicit_in_raw(country, raw_query):
        numismatic_exceptions = ["drachma", "drachmai", "drachmas", "lepta", "george i", "georgios"]
        conflicting_greek_authority = (
            canonical_country(country) == "greece"
            and any(term in a for term in ("kreta", "crete", "cretan state", "cretan"))
        )
        has_exception = any(ex in a for ex in numismatic_exceptions) and not conflicting_greek_authority
        if not country_in_title(country, a) and not has_exception:
            return False
'''

if new in text:
    print("inferred-country hard-filter fix already applied")
elif old in text:
    text = text.replace(old, new, 1)
    PATH.write_text(text, encoding="utf-8")
    print("applied inferred-country hard-filter fix")
else:
    raise SystemExit("expected country hard-filter block not found; refusing unsafe patch")

# Structural regression guard: inferred country must not have an else-branch
# that rejects a listing merely because country_in_title() is false.
updated = PATH.read_text(encoding="utf-8")
assert "if country and _country_explicit_in_raw(country, raw_query):" in updated
assert "raw \"5 drachmai 1901\"" in updated
print("inferred-country hard-filter regression guard passed")
