from enum import Enum


class EventSource(str, Enum):
    RUNTIME = "runtime"
    NHI = "nhi"
    REPO = "repo"
    SAAS = "saas"


class RiskTier(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class DataClass(str, Enum):
    PHI = "PHI"
    PCI = "PCI"
    PII = "PII"
    SECRETS = "secrets"
    CLAIMS_DATA = "claims_data"
    FINANCIAL = "financial"
    INTERNAL = "internal"
    PUBLIC = "public"


class Framework(str, Enum):
    LANGCHAIN = "langchain"
    LANGGRAPH = "langgraph"
    CREWAI = "crewai"
    LLAMA_INDEX = "llama_index"
    AUTOGEN = "autogen"
    MCP_AGENT = "mcp_agent"
    DIRECT_SDK_AGENTIC = "direct_sdk_agentic"
    LLM_CALLER = "llm_caller"
    UNKNOWN = "unknown"


AGENTIC_FRAMEWORKS = {
    Framework.LANGCHAIN,
    Framework.LANGGRAPH,
    Framework.CREWAI,
    Framework.LLAMA_INDEX,
    Framework.AUTOGEN,
    Framework.MCP_AGENT,
}


class Provider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    BEDROCK = "bedrock"
    AZURE_OPENAI = "azure_openai"
    OTHER = "other"


EXTERNAL_LLM_DOMAINS = {
    "api.anthropic.com": Provider.ANTHROPIC,
    "api.openai.com": Provider.OPENAI,
    "generativelanguage.googleapis.com": Provider.GOOGLE,
}


SENSITIVE_DATA_CLASSES = {
    DataClass.PHI,
    DataClass.PCI,
    DataClass.PII,
    DataClass.SECRETS,
    DataClass.CLAIMS_DATA,
    DataClass.FINANCIAL,
}
