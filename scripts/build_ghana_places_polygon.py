import json
import re
from collections import defaultdict
from shapely.geometry import shape, Point


def load_regions(path):
    """Load region polygons from GADM level 1."""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    regions = []
    for feature in data['features']:
        geom = shape(feature['geometry'])
        name = feature['properties'].get('NAME_1', '').strip()
        if name:
            regions.append((geom, name))
    print(f"Loaded {len(regions)} regions.")
    return regions


def load_districts(path, regions):
    """Load district polygons and determine their region using point‑in‑polygon."""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    districts = []
    for feature in data['features']:
        geom = shape(feature['geometry'])
        props = feature.get('properties', {})
        district_name = props.get('name', '').strip()
        if not district_name:
            continue

        # Find which region contains the district's centroid
        centroid = geom.centroid
        region_name = ''
        for r_geom, r_name in regions:
            if r_geom.contains(centroid):
                region_name = r_name
                break

        if not region_name:
            # Fallback: find the region that contains the most of the district
            best_intersection = 0
            for r_geom, r_name in regions:
                if geom.intersects(r_geom):
                    intersection = geom.intersection(r_geom).area
                    if intersection > best_intersection:
                        best_intersection = intersection
                        region_name = r_name
            if not region_name:
                print(f"  WARNING: Could not determine region for '{district_name}' — skipping")
                continue

        districts.append((geom, region_name, district_name))

    print(f"Loaded {len(districts)} valid districts (with region names).")
    return districts


def load_towns(path):
    """Load town nodes from an Overpass JSON export (raw data)."""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    towns = []
    for el in data['elements']:
        if el['type'] == 'node' and 'tags' in el and 'name' in el['tags']:
            towns.append((el['tags']['name'], el['lat'], el['lon']))
    print(f"Loaded {len(towns)} named places.")
    return towns


def fix_region_name(name: str) -> str:
    """Insert spaces before capital letters to split concatenated words,
    but keep common acronyms together. Also handle known special cases."""
    # Handle known multi‑word regions that may lose spaces
    corrections = {
        'GreaterAccra': 'Greater Accra',
        'UpperEast': 'Upper East',
        'UpperWest': 'Upper West',
        'NorthEast': 'North East',
        'WesternNorth': 'Western North',
        'BonoEast': 'Bono East',
        'BrongAhafo': 'Brong Ahafo',
        'Ahafo': 'Ahafo',            # single word, unchanged
    }
    if name in corrections:
        return corrections[name]

    # General rule: insert a space before an uppercase letter that follows a lowercase letter
    name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
    return name.strip()


def main():
    regions = load_regions('data/ghana_regions.geojson')
    districts = load_districts('data/ghana_districts.geojson', regions)
    towns = load_towns('data/ghana_raw.json')

    result = defaultdict(lambda: defaultdict(list))

    for town_name, lat, lon in towns:
        pt = Point(lon, lat)
        matched = None
        for poly, region, district in districts:
            if poly.contains(pt):
                matched = (region, district)
                break
        if matched:
            region, district = matched
        else:
            # Fallback to nearest district centroid
            best_dist = float('inf')
            best_region = best_district = ''
            for poly, region, district in districts:
                centroid = poly.centroid
                d = ((lon - centroid.x)**2 + (lat - centroid.y)**2)**0.5
                if d < best_dist:
                    best_dist = d
                    best_region = region
                    best_district = district
            region, district = best_region, best_district

        result[region][district].append({
            "name": town_name,
            "lat": lat,
            "lon": lon
        })

    # Build the raw output dictionary
    raw_output = {r: dict(ds) for r, ds in result.items() if r}

    # Fix region names (insert missing spaces)
    output = {}
    for region_name, districts_dict in raw_output.items():
        fixed_name = fix_region_name(region_name)
        output[fixed_name] = districts_dict

    with open('data/ghana_places.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"Saved data/ghana_places.json with {len(output)} regions.")


if __name__ == '__main__':
    main()