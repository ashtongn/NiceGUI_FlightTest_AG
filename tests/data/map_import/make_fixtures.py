"""Writes the binary fixture files for tests/test_map_import.py into this
directory (the KMZ, the two shapefile zips, the PMTiles header and the
MBTiles) and areas.kml from the same KML text as the KMZ. The other text
fixtures (route.geojson, track.gpx, the CSVs) are kept as written. Needs
pyshp for the shapefiles.

    python3 tests/data/map_import/make_fixtures.py
"""

import io
import os
import sqlite3
import struct
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
<Document><name>fixture</name>
<Placemark><name>Range A</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
-110.95,32.30,0 -110.90,32.30,0 -110.90,32.34,0 -110.95,32.34,0 -110.95,32.30,0
</coordinates></LinearRing></outerBoundaryIs>
<innerBoundaryIs><LinearRing><coordinates>-110.93,32.31,0 -110.92,32.31,0 -110.92,32.32,0 -110.93,32.31,0</coordinates></LinearRing></innerBoundaryIs>
</Polygon></Placemark>
<Placemark><name>Ingress</name><LineString><coordinates>-111.0,32.0,100 -110.9,32.1,100 -110.8,32.2,100</coordinates></LineString></Placemark>
<Placemark><name>Site 1</name><Point><coordinates>-110.97,32.22,0</coordinates></Point></Placemark>
<Placemark><name>Flight</name><gx:Track><when>2026-01-01T00:00:00Z</when><gx:coord>-111.1 32.5 300</gx:coord><gx:coord>-111.0 32.6 300</gx:coord></gx:Track></Placemark>
<Placemark><name>Both</name><MultiGeometry><Point><coordinates>-110.5,32.5,0</coordinates></Point><Point><coordinates>999,32.5,0</coordinates></Point></MultiGeometry></Placemark>
</Document></kml>
"""


def write_kmz():
    with zipfile.ZipFile(os.path.join(HERE, "areas.kmz"), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", KML)
        z.writestr("images/readme.txt", "nothing")


def write_shapefile_zip():
    import shapefile
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON) as w:
        w.field("NAME", "C", size=40)
        # clockwise outer ring, as shapefiles want it
        w.poly([[[-112.0, 33.0], [-112.0, 33.1], [-111.9, 33.1], [-111.9, 33.0], [-112.0, 33.0]]])
        w.record("Box one")
        w.poly([[[-111.5, 33.5], [-111.5, 33.6], [-111.4, 33.6], [-111.4, 33.5], [-111.5, 33.5]],
                [[-111.48, 33.52], [-111.42, 33.52], [-111.42, 33.58], [-111.48, 33.58], [-111.48, 33.52]]])
        w.record("Box with hole")
    pshp, pshx, pdbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=pshp, shx=pshx, dbf=pdbf, shapeType=shapefile.POINT) as w:
        w.field("label", "C", size=20)
        w.point(-111.7, 33.3)
        w.record("P1")
        w.point(-111.6, 33.4)
        w.record("P2")
    with zipfile.ZipFile(os.path.join(HERE, "areas_shp.zip"), "w") as z:
        z.writestr("areas.shp", shp.getvalue())
        z.writestr("areas.shx", shx.getvalue())
        z.writestr("areas.dbf", dbf.getvalue())
        z.writestr("areas.prj", 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]')
        z.writestr("sites.shp", pshp.getvalue())
        z.writestr("sites.shx", pshx.getvalue())
        z.writestr("sites.dbf", pdbf.getvalue())
    # a projected one the reader must refuse
    with zipfile.ZipFile(os.path.join(HERE, "projected_shp.zip"), "w") as z:
        z.writestr("utm.shp", shp.getvalue())
        z.writestr("utm.shx", shx.getvalue())
        z.writestr("utm.dbf", dbf.getvalue())
        z.writestr("utm.prj", 'PROJCS["WGS_1984_UTM_Zone_12N",GEOGCS["GCS_WGS_1984"]]')


def write_pmtiles():
    # a v3 header only: enough for the header reader, not a servable file
    h = bytearray(127)
    h[0:7] = b"PMTiles"
    h[7] = 3
    h[96] = 1           # clustered
    h[97] = 2           # internal compression gzip
    h[98] = 2           # tile compression gzip
    h[99] = 1           # tile type MVT
    h[100] = 0          # min zoom
    h[101] = 12         # max zoom
    struct.pack_into("<iiii", h, 102, -1148200000, 313300000, -1090400000, 370000000)
    h[118] = 6
    struct.pack_into("<ii", h, 119, -1119000000, 342000000)
    with open(os.path.join(HERE, "tiny.pmtiles"), "wb") as f:
        f.write(bytes(h) + b"\0" * 64)


def write_mbtiles():
    path = os.path.join(HERE, "tiny.mbtiles")
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name text, value text)")
    con.executemany("INSERT INTO metadata VALUES (?, ?)", [
        ("name", "tiny"), ("format", "pbf"), ("minzoom", "0"), ("maxzoom", "10"),
        ("bounds", "-114.82,31.33,-109.04,37.0")])
    con.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, "
                "tile_row integer, tile_data blob)")
    con.commit()
    con.close()


def write_kml():
    with open(os.path.join(HERE, "areas.kml"), "w", encoding="utf-8") as f:
        f.write(KML)


if __name__ == "__main__":
    write_kml()
    write_kmz()
    write_shapefile_zip()
    write_pmtiles()
    write_mbtiles()
    print("fixtures written to", HERE)
