// Dispatcher for all Spotify GET operations.
// Most endpoints have a single 'mock' example,
// so we always return 'mock'.
//
// Exception: GET /search — the response type depends on the 'type' query param:
//   type=album  → 'mock_album'   {"albums":  {"items": [{id, name, release_date, artists}]}}
//   type=artist → 'mock_artist'  {"artists": {"items": [{id, name}]}}
//   type=track  → 'mock'         {"tracks":  {"items": [{id, name, artists}]}}  (default)
//
// Microcks performs the operation routing first (path-based),
// then this script selects the example. For every endpoint other
// than /search the check on the 'type' param is harmless (missing → fallback 'mock').

def params = mockRequest.getQueryParameters()
def type   = params?.get('type')?.toString()?.toLowerCase() ?: ''

if (type == 'album')  return 'mock_album'
if (type == 'artist') return 'mock_artist'
return 'mock'