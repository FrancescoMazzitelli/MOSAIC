import groovy.json.JsonOutput
import org.yaml.snakeyaml.Yaml
import java.net.URL
import java.net.HttpURLConnection

def SERVICE_ID   = "spotify"
def SERVICE_NAME = "Spotify"
def API          = "Spotify"
def API_VERSION  = "1.0"
def OPENAPI_FILE = "Spotify-1.0.yaml"

def MICROCKS_HOST          = System.getenv("MICROCKS_HOST")          ?: "mock-server"
def MICROCKS_INTERNAL_PORT = System.getenv("MICROCKS_INTERNAL_PORT") ?: "8080"
def GATEWAY_HOST           = System.getenv("GATEWAY_HOST")           ?: "catalog-gateway"
def GATEWAY_PORT           = System.getenv("GATEWAY_PORT")           ?: "5000"
def CONSUL_HOST            = System.getenv("CONSUL_HOST")            ?: "registry"
def CONSUL_INTERNAL_PORT   = System.getenv("CONSUL_INTERNAL_PORT")   ?: "8500"

def withRetry = { int maxAttempts, long baseDelayMs, Closure action ->
    int attempt = 0
    while (true) {
        try { return action.call(attempt) }
        catch (Exception e) {
            attempt++
            if (attempt >= maxAttempts) throw e
            long delay = baseDelayMs * (1L << attempt)
            println "[retry ${attempt}/${maxAttempts-1}] ${e.message} — waiting ${delay}ms"
            Thread.sleep(delay)
        }
    }
}

def openApiUrl  = "http://${MICROCKS_HOST}:${MICROCKS_INTERNAL_PORT}/api/resources/${OPENAPI_FILE}"
def yamlContent = new URL(openApiUrl).text
def swaggerMap  = new Yaml().load(yamlContent)

def description = swaggerMap?.info?.description ?: "Spotify music player service."
def paths       = swaggerMap?.paths ?: [:]
def host        = swaggerMap?.servers?.getAt(0)?.url ?: "http://${MICROCKS_HOST}:${MICROCKS_INTERNAL_PORT}"

def parsedUrl = new URL(host)
def hostUrl   = parsedUrl.host + (parsedUrl.port != -1 ? ":${parsedUrl.port}" : "")
def base      = parsedUrl.path

def capabilities = [:]
def endpoints    = [:]

paths.each { path, methods ->
    if (!(methods instanceof Map)) return
    methods.each { method, details ->
        def endpointKey          = "${method.toUpperCase()} ${path}"
        def desc                 = details?.description ?: details?.summary ?: details?.operationId ?: method
        capabilities[endpointKey] = desc
        endpoints[endpointKey]    = "http://${hostUrl}${base}${path}"
    }
}

def catalogPayload = [
    id: SERVICE_ID, name: SERVICE_NAME,
    description: description,
    capabilities: capabilities, endpoints: endpoints
]

def consulPayload = [
    Name: SERVICE_NAME, Id: SERVICE_ID,
    Meta: [service_doc_id: SERVICE_ID],
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

try {
    def c = new URL("http://${GATEWAY_HOST}:${GATEWAY_PORT}/service/${SERVICE_ID}").openConnection() as HttpURLConnection
    c.setRequestMethod("DELETE"); c.connect()
    println "[deregister] Gateway HTTP ${c.responseCode}"
} catch (Exception e) { println "[deregister] Gateway skip: ${e.message}" }

try {
    def c = new URL("http://${CONSUL_HOST}:${CONSUL_INTERNAL_PORT}/v1/agent/service/deregister/${SERVICE_ID}").openConnection() as HttpURLConnection
    c.setRequestMethod("PUT"); c.connect()
    println "[deregister] Consul HTTP ${c.responseCode}"
} catch (Exception e) { println "[deregister] Consul skip: ${e.message}" }

try {
    withRetry(3, 2000L) { i ->
        def c = new URL("http://${GATEWAY_HOST}:${GATEWAY_PORT}/service").openConnection() as HttpURLConnection
        c.setRequestMethod("POST"); c.setDoOutput(true)
        c.setRequestProperty("Content-Type","application/json")
        c.outputStream.withWriter("UTF-8") { it << jsonCatalog }
        def code = c.responseCode; println "[gateway] HTTP ${code}"
        if (code >= 400) throw new RuntimeException("HTTP ${code}")
    }
    responseMap.status = "success"
} catch (Exception e) { println "[gateway] FAILED: ${e.message}"; return "fail" }

try {
    withRetry(3, 2000L) { i ->
        def c = new URL("http://${CONSUL_HOST}:${CONSUL_INTERNAL_PORT}/v1/agent/service/register").openConnection() as HttpURLConnection
        c.setRequestMethod("PUT"); c.setDoOutput(true)
        c.setRequestProperty("Content-Type","application/json")
        c.outputStream.withWriter("UTF-8") { it << jsonConsul }
        def code = c.responseCode; println "[consul] HTTP ${code}"
        if (code >= 400) throw new RuntimeException("HTTP ${code}")
        responseMap.status = "success"
    }
} catch (Exception e) { println "[consul] FAILED: ${e.message}"; return "fail" }

println "[spotify] ✅ ${capabilities.size()} endpoints registered"
return "ok"