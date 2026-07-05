# Bundling the MCAT deck with the installer

To pre-load the MCAT flashcard deck for new users, export it from your
collection and place the file here before building:

    qt/aqt/speedrun/mcat_deck.apkg

**How to export:**
1. Open Anki → select the top-level `AnKing-MCAT` deck
2. File → Export
3. Format: **Anki Deck Package (.apkg)**
4. Check **Include scheduling information** if you want to bundle your own
   review history (usually leave it unchecked so new users start fresh)
5. Save as `mcat_deck.apkg` in this folder

On first launch, the app will detect that the `AnKing-MCAT` deck is missing
and silently import `mcat_deck.apkg` in the background.  If the file is not
present, the app still starts normally — users can import decks themselves.
