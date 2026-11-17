/** Trimmed real response shapes (probed 2026-11-12), one per browser source, for parser tests. */
export const FIXTURES: Record<string, unknown> = {
  usgs: {
    features: [
      {
        id: 'us7000txqr',
        properties: {
          title: 'M 5.4 - South Sandwich Islands region',
          time: 1794455156644,
          url: 'https://earthquake.usgs.gov/earthquakes/eventpage/us7000txqr',
        },
        geometry: { type: 'Point', coordinates: [-26.03, -59.03, 121.5] },
      },
      { id: 'bad', properties: {} },
    ],
  },
  eonet: {
    events: [
      {
        id: 'EONET_26159',
        title: 'Tropical Cyclone 03S',
        link: 'https://eonet.gsfc.nasa.gov/api/v3/events/EONET_26159',
        categories: [{ id: 'severeStorms', title: 'Severe Storms' }],
        sources: [
          { id: 'JTWC', url: 'https://www.metoc.navy.mil/jtwc/products/sh0327.tcw' },
          { id: 'x', url: 'javascript:alert(1)' },
        ],
        geometry: [
          { date: '2026-11-07T18:00:00Z', type: 'Point', coordinates: [52.6, -13.5] },
          { date: '2026-11-08T00:00:00Z', type: 'Point', coordinates: [52.3, -13.4] },
        ],
      },
    ],
  },
  gdacs: {
    features: [
      {
        geometry: { type: 'Point', coordinates: [-26.0288, -59.0326] },
        properties: {
          eventtype: 'EQ',
          eventid: 1576913,
          name: 'Earthquake in South Sandwich Islands Region',
          fromdate: '2026-11-12T03:45:56',
          alertlevel: 'Green',
          url: { report: 'https://www.gdacs.org/report.aspx?eventid=1576913&episodeid=1746122&eventtype=EQ' },
          description: 'Green M 5.4',
        },
      },
    ],
  },
  nws_alerts: {
    features: [
      {
        geometry: {
          type: 'Polygon',
          coordinates: [
            [
              [-84.72, 36.91],
              [-84.75, 37.09],
              [-83.95, 37.06],
              [-84.72, 36.91],
            ],
          ],
        },
        properties: {
          id: 'urn:oid:2.49.0.1.840.0.111',
          '@id': 'https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.111',
          event: 'Special Weather Statement',
          headline: 'Special Weather Statement issued November 11 at 11:15PM EST by NWS Jackson KY',
          sent: '2026-11-11T23:15:00-05:00',
          severity: 'Moderate',
          areaDesc: 'Pulaski; Laurel',
        },
      },
    ],
  },
  hackernews: {
    hits: [
      {
        objectID: '50450488',
        title: 'Tim Peters has died',
        url: 'https://discuss.python.org/t/in-memoriam-tim-peters-1952-2026/110028',
        created_at: '2026-11-11T07:03:17Z',
      },
      { objectID: '1', title: 'Ask HN', url: null, created_at: '2026-11-11T07:03:17Z' },
    ],
  },
  nvd: {
    vulnerabilities: [
      {
        cve: {
          id: 'CVE-2026-0001',
          published: '2026-11-11T10:00:00.000',
          descriptions: [
            { lang: 'es', value: 'hola' },
            { lang: 'en', value: 'Weak encryption in admin store.' },
          ],
          metrics: { cvssMetricV31: [{ cvssData: { baseSeverity: 'HIGH' } }] },
        },
      },
    ],
  },
  devto: [
    {
      id: 5015589,
      title: 'We Keep Hearing About AI Productivity',
      url: 'https://dev.to/x/we-keep-hearing',
      published_timestamp: '2026-11-11T08:35:15Z',
      description: 'An anonymous poll',
      tag_list: ['ai', 'qa'],
    },
  ],
  github_releases: [
    {
      id: 442294074,
      name: '',
      tag_name: 'v16.5.0-canary.48',
      html_url: 'https://github.com/vercel/next.js/releases/tag/v16.5.0-canary.48',
      published_at: '2026-11-11T20:00:00Z',
      body: 'Canary release',
    },
  ],
  coingecko: [
    {
      id: 'bitcoin',
      name: 'Bitcoin',
      symbol: 'btc',
      current_price: 73669,
      price_change_percentage_24h: 0.31439,
      last_updated: '2026-11-12T04:21:50.000Z',
    },
  ],
  fear_greed: { data: [{ value: '28', value_classification: 'Fear', timestamp: '1794441600' }] },
  launches: {
    results: [
      {
        id: 'd66590ab',
        name: 'Long March 2D | CAS Pathfinder-1',
        net: '2026-11-11T11:41:00Z',
        url: 'https://ll.thespacedevs.com/2.2.0/launch/d66590ab/',
        slug: 'long-march-2d-cas-pathfinder-1',
        status: { name: 'Launch Successful' },
        pad: { name: 'Launch Area 94', latitude: '40.960482', longitude: '100.298059' },
      },
    ],
  },
  freetogame: [
    {
      id: 655,
      title: 'Steel Aces',
      game_url: 'https://www.freetogame.com/open/steel-aces',
      short_description: 'A free-to-play tank MMO.',
      genre: 'Shooter',
      release_date: '2026-10-30',
    },
  ],
  gamerpower: [
    {
      id: 3849,
      title: 'Kettlebell DLC Key Giveaway',
      open_giveaway_url: 'https://www.gamerpower.com/open/kettlebell',
      description: 'Claim your key',
      published_date: '2026-11-10 13:29:13',
      type: 'DLC',
    },
  ],
};
