use base64::{engine::general_purpose::STANDARD, Engine};
use nfsembed::vfs::NfsError;
use reqwest::{header, redirect::Policy, Client, Url};
use serde::{Deserialize, Serialize};
use std::time::Duration;

pub const CHUNK: u32 = 131_072;
pub const MAX_FILE_BYTES: u64 = 2_147_483_647;
const RESPONSE_LIMIT: usize = 2 * 1024 * 1024;

#[derive(Default, Serialize)]
pub struct Request<'a> {
    pub op: &'a str,
    pub path: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub destination: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub offset: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub count: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mode: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exclusive: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verifier: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub directory: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub modified_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub guard_modified_at: Option<String>,
}

impl<'a> Request<'a> {
    pub fn new(op: &'a str, path: &'a str) -> Self {
        Self {
            op,
            path,
            ..Default::default()
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    pub path: String,
    pub is_dir: bool,
    pub size: u64,
    pub modified_at: Option<String>,
    pub mode: u32,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Response {
    pub ok: bool,
    pub entry: Option<Entry>,
    pub entries: Option<Vec<Entry>>,
    pub data: Option<String>,
    pub written: Option<u32>,
    pub eof: Option<bool>,
    pub errno: Option<u32>,
    pub message: Option<String>,
}

pub struct Gateway {
    url: Url,
    client: Client,
}

impl Gateway {
    pub fn new(endpoint: &str, token: &str) -> Result<Self, &'static str> {
        let url = Url::parse(endpoint).map_err(|_| "invalid gateway URL")?;
        if !matches!(url.scheme(), "http" | "https")
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || token.is_empty()
        {
            return Err("invalid gateway configuration");
        }
        let mut auth = header::HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|_| "invalid session token")?;
        auth.set_sensitive(true);
        let mut headers = header::HeaderMap::new();
        headers.insert(header::AUTHORIZATION, auth);
        let client = Client::builder()
            .default_headers(headers)
            .redirect(Policy::none())
            .timeout(Duration::from_secs(15))
            .connect_timeout(Duration::from_secs(5))
            .build()
            .map_err(|_| "cannot initialize HTTP transport")?;
        Ok(Self { url, client })
    }

    pub async fn call(&self, request: &Request<'_>) -> Result<Response, NfsError> {
        // Unknown mutation acceptance is never retried by this adapter.
        let mut response = self
            .client
            .post(self.url.clone())
            .json(request)
            .send()
            .await
            .map_err(|_| NfsError::Io)?;
        if matches!(response.status().as_u16(), 401 | 403) {
            return Err(NfsError::Access);
        }
        if !response.status().is_success() {
            return Err(NfsError::Io);
        }
        let mut body = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|_| NfsError::Io)? {
            if body.len().saturating_add(chunk.len()) > RESPONSE_LIMIT {
                return Err(NfsError::Io);
            }
            body.extend_from_slice(&chunk);
        }
        let result: Response = serde_json::from_slice(&body).map_err(|_| NfsError::Io)?;
        if !result.ok {
            return Err(errno(result.errno.ok_or(NfsError::Io)?));
        }
        if result.errno.is_some() || result.message.is_some() {
            return Err(NfsError::Io);
        }
        Ok(result)
    }
}

pub fn encode(bytes: &[u8]) -> String {
    STANDARD.encode(bytes)
}
pub fn decode(text: &str) -> Result<Vec<u8>, NfsError> {
    STANDARD.decode(text).map_err(|_| NfsError::Io)
}

fn errno(value: u32) -> NfsError {
    match value {
        1 => NfsError::Permission,
        2 => NfsError::NotFound,
        13 => NfsError::Access,
        17 => NfsError::Exists,
        18 => NfsError::CrossDevice,
        20 => NfsError::NotDirectory,
        21 => NfsError::IsDirectory,
        22 => NfsError::Invalid,
        27 => NfsError::FileTooLarge,
        28 => NfsError::NoSpace,
        30 => NfsError::ReadOnly,
        36 => NfsError::NameTooLong,
        39 => NfsError::NotEmpty,
        95 => NfsError::NotSupported,
        116 => NfsError::Stale,
        10002 => NfsError::NotSynchronized,
        _ => NfsError::Io,
    }
}
