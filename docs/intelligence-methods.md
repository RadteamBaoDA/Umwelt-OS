# Intelligence and map evidence methods

## Temporal co-occurrence (`co_occurrence_v1`)

The projection groups owner-authorized event evidence and current provider observations by their recorded region and UTC hour. It retains source evidence identifiers, caps each event-domain scan and returns coverage/truncation metadata. The economic observation input is limited to explicitly selected active Alpha Vantage sources. It does not infer missing regions, match event text, estimate statistical significance, rank risk, predict outcomes, or establish causation. A shared time bucket means only that the recorded signals co-occurred in time and region. Coverage gaps and truncation are part of the result and must be shown with it.

## Cyber Incident Index v8

The CII v8 projection currently returns `score`, `band`, `movement_24h`, and `as_of` as null with availability `method_data_license_unverified`. No country score, 31-country set, ranking, or movement is synthesized. Enablement requires a verified authoritative v8 method, exact country coverage, and a permitted data/license source.

## Map coordinates and geometry

Map points come only from the owner-public geospatial observation projection, after source generation, provider scope, evidence currency, time range, and region filtering. Provider-recorded coordinates remain hidden until a user opts in to precise-location disclosure. The map does not geocode names or request remote tiles/provider data from the browser. Unsupported military, economic, disaster, and escalation layers remain unavailable until a permitted typed geospatial adapter is implemented.

The local flat basemap uses `world-atlas` 2.0.2 TopoJSON derived from Natural Earth 1:110m data; Natural Earth data is public domain. Package and dataset attribution and license are recorded in `OSS_USED.md` and `THIRD_PARTY_NOTICES.md`. Globe and flat engines share layer descriptors, feature IDs, and selection state; the accessible evidence list remains available independently of WebGL.

## Reference-only products

WorldMonitor and GOIES remain reference-only. No code, data, or scores from either project are included in these methods or projections.
