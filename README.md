# Cmentarz parafialny w Siedliszczu – wirtualny cmentarz

Strona 3D z wyszukiwarką grobów, zdjęciami grobów, wirtualnymi zniczami i zamawianiem usług przy grobach.

- Dane (lista pochowanych, położenie i zdjęcia grobów) pochodzą z ewidencji cmentarza w serwisie cmentarz.app i są publikowane **za pisemną zgodą zarządcy cmentarza**.
- Automat (`.github/workflows/update.yml`) co niedzielę w nocy pobiera aktualne dane i publikuje stronę na GitHub Pages.
- Ręczne odświeżenie: zakładka **Actions** → **Aktualizacja cmentarza** → **Run workflow**.

Pliki:
- `site/index.html` – strona,
- `scripts/update.py` – pobieranie danych i zdjęć oraz budowa strony do folderu `_site`.
