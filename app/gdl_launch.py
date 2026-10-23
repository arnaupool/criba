"""Lanza gallery-dl con arreglos pendientes de publicar en gallery-dl (se usa como `python -m app.gdl_launch`).

Cada arreglo solo actúa si hace falta: si gallery-dl ya lo trae, no cambia nada, y si su código cambia
tanto que el arreglo no encaja, se ignora y gallery-dl arranca tal cual.
"""
import sys


def _instagram_seo_variable():
    """Instagram exige desde el 21-oct-2026 la variable PolarisSEOIsCrawler en las consultas de publicaciones
    y etiquetadas; sin ella responde «missing_required_variable_value» (KeyError: 'data').
    https://codeberg.org/mikf/gallery-dl/issues/548 · https://codeberg.org/mikf/gallery-dl/issues/552"""
    from gallery_dl.extractor import instagram

    ops = {"PolarisProfilePostsTabContentQuery_connection", "PolarisProfileTaggedTabContentQuery_connection"}
    key = "__relay_internal__pv__PolarisSEOIsCrawlerrelayprovider"
    original = instagram.InstagramAPI._pagination_graphql

    def patched(self, opname, variables, *args, **kwargs):
        if opname in ops and isinstance(variables, dict):
            variables.setdefault(key, False)
        return original(self, opname, variables, *args, **kwargs)

    instagram.InstagramAPI._pagination_graphql = patched


for fix in (_instagram_seo_variable,):
    try:
        fix()
    except Exception:  # noqa: BLE001 — si gallery-dl cambia, mejor sin arreglo que sin gallery-dl
        pass

if __name__ == "__main__":
    from gallery_dl import main
    sys.exit(main())
