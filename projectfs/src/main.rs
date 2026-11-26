use nfsembed::{AuthPolicy, NfsServer, ServerLimits};
use projectfs_export::{
    rpc::{Gateway, CHUNK},
    ProjectFs,
};
use std::{env, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

#[tokio::main]
async fn main() {
    if run().await.is_err() {
        eprintln!("project filesystem exporter failed");
        std::process::exit(1);
    }
}
async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let listen = env::var("PROJECTFS_LISTEN").unwrap_or_else(|_| "0.0.0.0:2049".into());
    if env::args().nth(1).as_deref() == Some("healthcheck") {
        let address: std::net::SocketAddr = listen.parse()?;
        let port = address.port();
        tokio::time::timeout(Duration::from_secs(2), healthcheck(port)).await??;
        return Ok(());
    }
    let gateway = Gateway::new(
        &env::var("PROJECTFS_RPC_URL")?,
        &env::var("PROJECTFS_TOKEN")?,
    )?;
    let uid = env::var("PROJECTFS_UID")
        .unwrap_or_else(|_| "1000".into())
        .parse()?;
    let gid = env::var("PROJECTFS_GID")
        .unwrap_or_else(|_| "1000".into())
        .parse()?;
    let fs = ProjectFs::new(
        gateway,
        uid,
        gid,
        env::var("PROJECTFS_READ_ONLY").as_deref() == Ok("1"),
    );
    let limits = ServerLimits {
        max_connections: 32,
        max_inflight_requests: 64,
        max_requests_per_connection: 8,
        max_read_size: CHUNK,
        max_write_size: CHUNK,
        max_readdir_response_size: CHUNK,
        max_rpc_record_size: 262144,
        max_rpc_fragment_size: 262144,
        max_buffered_request_bytes: 8 * 1024 * 1024,
        max_buffered_reply_bytes: 8 * 1024 * 1024,
        request_timeout: Duration::from_secs(20),
        ..ServerLimits::default()
    };
    let server = NfsServer::builder(fs)
        .auth_policy(AuthPolicy::AuthSysOrAnonymous)
        .limits(limits)
        .build()?;
    let listener = TcpListener::bind(listen).await?;
    server.serve(listener, async {
        #[cfg(unix)] {
            match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
                Ok(mut terminate) => { tokio::select! { _ = terminate.recv() => {}, _ = tokio::signal::ctrl_c() => {} } }
                Err(_) => { let _ = tokio::signal::ctrl_c().await; }
            }
        }
        #[cfg(not(unix))] { let _ = tokio::signal::ctrl_c().await; }
    }).await?;
    Ok(())
}

async fn healthcheck(port: u16) -> Result<(), Box<dyn std::error::Error>> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).await?;
    let words = [42_u32, 0, 2, 100003, 3, 0, 1, 20, 0, 0, 1000, 1000, 0, 0, 0];
    let mut call = Vec::new();
    call.extend_from_slice(&(0x80000000_u32 | (words.len() as u32 * 4)).to_be_bytes());
    for word in words {
        call.extend_from_slice(&word.to_be_bytes());
    }
    stream.write_all(&call).await?;
    let marker = stream.read_u32().await?;
    let size = marker & 0x7fffffff;
    if marker >> 31 != 1 || !(24..=512).contains(&size) {
        return Err("invalid NFS health reply".into());
    }
    let mut reply = vec![0; size as usize];
    stream.read_exact(&mut reply).await?;
    let expected = [42_u32, 1, 0, 0, 0, 0];
    for (index, word) in expected.iter().enumerate() {
        if reply[index * 4..index * 4 + 4] != word.to_be_bytes() {
            return Err("NFS NULL rejected".into());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn healthcheck_requires_valid_nfs_null_reply_not_only_open_port() {
        for valid in [false, true] {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let port = listener.local_addr().unwrap().port();
            let task = tokio::spawn(async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut call = [0; 64];
                stream.read_exact(&mut call).await.unwrap();
                let mut reply = vec![];
                reply.extend_from_slice(&0x80000018_u32.to_be_bytes());
                for word in [if valid { 42_u32 } else { 99 }, 1, 0, 0, 0, 0] {
                    reply.extend_from_slice(&word.to_be_bytes());
                }
                stream.write_all(&reply).await.unwrap();
            });
            assert_eq!(healthcheck(port).await.is_ok(), valid);
            task.await.unwrap();
        }
    }
}
