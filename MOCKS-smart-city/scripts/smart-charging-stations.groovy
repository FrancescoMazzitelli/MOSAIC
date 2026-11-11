import groovy.json.JsonOutput
import org.yaml.snakeyaml.Yaml
import java.net.URL
import java.net.HttpURLConnection

// ── Service constants ──────────────────────────────────────────────────────
def SERVICE_NAME   = "smart-charging-stations-api-mock"
def SERVICE_ID     = "smart-charging-stations-mock"
def MICROCKS_INTERNAL_PORT = 8080
def MICROCKS_HOST  = "mock-server"
def CONSUL_INTERNAL_PORT   = 8500
def CONSUL_HOST    = "registry"
def GATEWAY_HOST   = "catalog-gateway"
def GATEWAY_PORT   = 5000
def API            = "Smart+Charging+Stations+API"
def OPENAPI_FILE   = "Smart%20Charging%20Stations%20API-1.0.yaml"
def API_VERSION    = "1.0"

// ── Retry helper ──────────────────────────────────────────────────────────
// Executes the given closure up to maxAttempts times.
// On failure waits baseDelayMs * 2^attempt milliseconds before retrying.
def withRetry = { int maxAttempts, long baseDelayMs, Closure action ->
    int attempt = 0
    while (true) {
        try {
            return action.call(attempt)
        } catch (Exception e) {
            attempt++
            if (attempt >= maxAttempts) throw e
            long delay = baseDelayMs * (1L << attempt)   // 2s, 4s, 8s …
            println "[retry ${attempt}/${maxAttempts - 1}] ${e.message} — waiting ${delay}ms"
            Thread.sleep(delay)
        }
    }
}

// ── Load OpenAPI spec from Microcks ──────────────────────────────────────
def openApiUrl  = "http://${MICROCKS_HOST}:${MICROCKS_INTERNAL_PORT}/api/resources/${OPENAPI_FILE}"
def yamlContent = new URL(openApiUrl).text

def yaml       = new Yaml()
def swaggerMap = yaml.load(yamlContent)

def description = swaggerMap.info?.description ?: "No description"
def paths       = swaggerMap.paths ?: [:]
def host        = swaggerMap.servers?.getAt(0)?.url ?: "http://${MICROCKS_HOST}:${MICROCKS_INTERNAL_PORT}"

def parsedUrl = new URL(host)
def hostUrl   = parsedUrl.host + (parsedUrl.port != -1 ? ":${parsedUrl.port}" : "")
def base      = parsedUrl.path

def capabilities = [:]
def endpoints    = [:]

paths.each { path, methods ->
    methods.each { method, details ->
        def endpointKey          = "${method.toUpperCase()} ${path}"
        def desc                 = details.description ?: details.summary ?: details.operationId ?: method
        capabilities[endpointKey] = desc
        endpoints[endpointKey]    = "http://${hostUrl}${base}${path}"
    }
}

// ── Build payloads ────────────────────────────────────────────────────────
def catalogPayload = [
    id          : SERVICE_ID,
    name        : SERVICE_NAME,
    description : description,
    capabilities: capabilities,
    endpoints   : endpoints
]

def consulPayload = [
    Name: SERVICE_NAME,
    Id  : SERVICE_ID,
    Meta: [ service_doc_id: SERVICE_ID ],
    Check: [
        TlsSkipVerify                : true,
        Method                       : "GET",
        Http                         : "http://${MICROCKS_HOST}:${MICROCKS_INTERNAL_PORT}/rest/${API}/${API_VERSION}/health",
        Interval                     : "10s",
        Timeout                      : "5s",
        DeregisterCriticalServiceAfter: "30s"
    ]
]

def jsonConsul  = JsonOutput.prettyPrint(JsonOutput.toJson(consulPayload))
def jsonCatalog = JsonOutput.prettyPrint(JsonOutput.toJson(catalogPayload))

def responseMap = [:]

// ── 1. Deregister from Gateway (best-effort) ──────────────────────────────
try {
    def deregGateway = new URL("http://${GATEWAY_HOST}:${GATEWAY_PORT}/service/${SERVICE_ID}")
    def conn = deregGateway.openConnection() as HttpURLConnection
    conn.setRequestMethod("DELETE")
    conn.connect()
    println "[deregister] Gateway responded HTTP ${conn.responseCode} for ${SERVICE_ID}"
} catch (Exception e) {
    println "[deregister] Gateway deregistration skipped: ${e.message}"
}

// ── 2. Deregister from Consul (best-effort) ───────────────────────────────
try {
    def deregConsul = new URL("http://${CONSUL_HOST}:${CONSUL_INTERNAL_PORT}/v1/agent/service/deregister/${SERVICE_ID}")
    def conn = deregConsul.openConnection() as HttpURLConnection
    conn.setRequestMethod("PUT")
    conn.connect()
    println "[deregister] Consul responded HTTP ${conn.responseCode} for ${SERVICE_ID}"
} catch (Exception e) {
    println "[deregister] Consul deregistration skipped: ${e.message}"
}

// ── 3. Register to Gateway (with retry) ───────────────────────────────────
try {
    withRetry(3, 2000L) { int attempt ->
        println "[gateway] Registration attempt ${attempt + 1}…"
        def gwUrl = new URL("http://${GATEWAY_HOST}:${GATEWAY_PORT}/service")
        def conn  = gwUrl.openConnection() as HttpURLConnection
        conn.setRequestMethod("POST")
        conn.setDoOutput(true)
        conn.setRequestProperty("Content-Type", "application/json")
        conn.outputStream.withWriter("UTF-8") { it << jsonCatalog }

        def code = conn.responseCode
        println "[gateway] HTTP ${code}"
        if (code >= 400) throw new RuntimeException("Gateway returned HTTP ${code}")
    }
} catch (Exception e) {
    println "[gateway] Registration failed after retries: ${e.message}"
    responseMap.status       = "error"
    responseMap.gatewayError = e.message
    return "fail"
}

// ── 4. Register to Consul (with retry) ────────────────────────────────────
try {
    withRetry(3, 2000L) { int attempt ->
        println "[consul] Registration attempt ${attempt + 1}…"
        def consulUrl = new URL("http://${CONSUL_HOST}:${CONSUL_INTERNAL_PORT}/v1/agent/service/register")
        def conn      = consulUrl.openConnection() as HttpURLConnection
        conn.setRequestMethod("PUT")
        conn.setDoOutput(true)
        conn.setRequestProperty("Content-Type", "application/json")
        conn.outputStream.withWriter("UTF-8") { it << jsonConsul }

        def code = conn.responseCode
        println "[consul] HTTP ${code}"
        if (code >= 400) throw new RuntimeException("Consul returned HTTP ${code}")

        responseMap.status   = "success"
        responseMap.httpCode = code
        responseMap.message  = "Service registered successfully"
    }
} catch (Exception e) {
    println "[consul] Registration failed after retries: ${e.message}"
    responseMap.status   = "error"
    responseMap.httpCode = 500
    responseMap.message  = "Failed to register to Consul"
    responseMap.error    = e.message
}

if (responseMap.status == "success") {
    return "ok"  // Returns the name of the success example in the YAML
} else {
    return "fail" // Returns the name of the failure example in the YAML
}
