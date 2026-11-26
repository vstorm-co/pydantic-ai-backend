use nfsembed::vfs::*;
use projectfs_export::{rpc::Gateway, ProjectFs};
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    sync::Notify,
    task::JoinHandle,
};

struct Fake {
    url: String,
    seen: Arc<Mutex<Vec<Value>>>,
    task: JoinHandle<()>,
    release: Arc<Notify>,
    arrived: Arc<Notify>,
    connections: Arc<Mutex<Vec<JoinHandle<()>>>>,
}
impl Drop for Fake {
    fn drop(&mut self) {
        self.task.abort();
        for task in self.connections.lock().unwrap().iter() {
            task.abort();
        }
    }
}
impl Fake {
    async fn new(replies: Vec<(u16, String, Duration)>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/fs", listener.local_addr().unwrap());
        let seen = Arc::new(Mutex::new(Vec::new()));
        let captured = seen.clone();
        let replies = Arc::new(Mutex::new(VecDeque::from(replies)));
        let release = Arc::new(Notify::new());
        let arrived = Arc::new(Notify::new());
        let release_server = release.clone();
        let arrived_server = arrived.clone();
        let connections = Arc::new(Mutex::new(Vec::new()));
        let active = connections.clone();
        let task = tokio::spawn(async move {
            loop {
                let (mut stream, _) = listener.accept().await.unwrap();
                let captured = captured.clone();
                let replies = replies.clone();
                let release = release_server.clone();
                let arrived = arrived_server.clone();
                let connection = tokio::spawn(async move {
                    let mut data = Vec::new();
                    let mut byte = [0];
                    while !data.ends_with(b"\r\n\r\n") {
                        stream.read_exact(&mut byte).await.unwrap();
                        data.push(byte[0]);
                        assert!(data.len() < 8192);
                    }
                    let headers = String::from_utf8(data).unwrap().to_ascii_lowercase();
                    assert!(headers.contains("authorization: bearer synthetic-token"));
                    let size: usize = headers
                        .lines()
                        .find_map(|line| line.strip_prefix("content-length: "))
                        .unwrap()
                        .parse()
                        .unwrap();
                    let mut body = vec![0; size];
                    stream.read_exact(&mut body).await.unwrap();
                    captured
                        .lock()
                        .unwrap()
                        .push(serde_json::from_slice(&body).unwrap());
                    arrived.notify_waiters();
                    let (status, body, delay) = replies
                        .lock()
                        .unwrap()
                        .pop_front()
                        .expect("unexpected HTTP call");
                    if delay == Duration::MAX {
                        release.notified().await;
                    } else {
                        tokio::time::sleep(delay).await;
                    }
                    let response = format!("HTTP/1.1 {status} Result\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
                    let _ = stream.write_all(response.as_bytes()).await;
                });
                active.lock().unwrap().push(connection);
            }
        });
        Self {
            url,
            seen,
            task,
            release,
            arrived,
            connections,
        }
    }
    async fn wait_count(&self, count: usize) {
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                let arrived = self.arrived.notified();
                if self.requests().len() >= count {
                    break;
                }
                arrived.await;
            }
        })
        .await
        .unwrap();
    }
    fn fs(&self, read_only: bool) -> ProjectFs {
        ProjectFs::new(
            Gateway::new(&self.url, "synthetic-token").unwrap(),
            1000,
            1000,
            read_only,
        )
    }
    fn requests(&self) -> Vec<Value> {
        self.seen.lock().unwrap().clone()
    }
}
fn response(value: Value) -> (u16, String, Duration) {
    (200, value.to_string(), Duration::ZERO)
}
fn entry(path: &str, directory: bool) -> Value {
    json!({"path":path,"is_dir":directory,"size":0,"mode":420,"modified_at":"2026-11-26T12:00:00Z"})
}
fn stat(path: &str, directory: bool) -> (u16, String, Duration) {
    response(json!({"ok":true,"entry":entry(path,directory)}))
}
fn context() -> RequestContext {
    RequestContext {
        principal: Principal::Anonymous,
        client_addr: "127.0.0.1:1".parse().unwrap(),
        export_id: ExportId(1),
    }
}
fn name(text: &str) -> NfsName {
    NfsName::new(text.as_bytes()).unwrap()
}

#[tokio::test]
async fn read_write_revoke_and_exclusive_contract() {
    let fake = Fake::new(vec![
        stat("file", false),
        response(json!({"ok":true,"data":"b25l","eof":false})),
        response(json!({"ok":true,"data":"dHdv","eof":true})),
        (
            200,
            json!({"ok":true,"written":3}).to_string(),
            Duration::MAX,
        ),
        response(json!({"ok":false,"errno":13,"message":"revoked"})),
    ])
    .await;
    let fs = Arc::new(fake.fs(false));
    let ctx = context();
    let key = fs
        .lookup(&ctx, fs.root(), &name("file"))
        .await
        .unwrap()
        .object;
    assert_eq!(fs.read(&ctx, key, 7, 3).await.unwrap().data, b"one");
    assert_eq!(fs.read(&ctx, key, 7, 3).await.unwrap().data, b"two");
    let worker = fs.clone();
    let task = tokio::spawn(async move {
        worker
            .write(&context(), key, 10, b"abc", WriteStability::Unstable)
            .await
    });
    fake.wait_count(4).await;
    assert!(!task.is_finished());
    fake.release.notify_one();
    let result = task.await.unwrap().unwrap();
    assert_eq!(result.value.committed, WriteStability::FileSync);
    assert_eq!(
        fs.read(&ctx, key, 0, 1).await.unwrap_err(),
        NfsError::Access
    );
    let seen = fake.requests();
    assert_eq!(seen.len(), 5);
    assert_eq!(seen[1]["offset"], 7);
    assert_eq!(seen[3]["data"], "YWJj");
}

#[tokio::test]
async fn exclusive_verifier_guarded_setattr_and_rename_preserve_handles() {
    let fake = Fake::new(vec![
        stat("old", false),
        response(json!({"ok":true})),
        stat("new", false),
        response(json!({"ok":true})),
        response(json!({"ok":false,"errno":17,"message":"exists"})),
        response(json!({"ok":false,"errno":10002,"message":"guard mismatch"})),
    ])
    .await;
    let fs = fake.fs(false);
    let ctx = context();
    let key = fs
        .create(
            &ctx,
            fs.root(),
            &name("old"),
            SetAttributes::default(),
            CreateMode::Exclusive {
                verifier: *b"12345678",
            },
        )
        .await
        .unwrap()
        .value
        .object;
    fs.rename(&ctx, fs.root(), &name("old"), fs.root(), &name("new"))
        .await
        .unwrap();
    fs.getattr(&ctx, key).await.unwrap();
    fs.setattr(
        &ctx,
        key,
        SetAttributes {
            size: Some(0),
            mode: Some(0),
            ..Default::default()
        },
        None,
    )
    .await
    .unwrap();
    assert_eq!(
        fs.create(
            &ctx,
            fs.root(),
            &name("new"),
            SetAttributes::default(),
            CreateMode::Guarded
        )
        .await
        .unwrap_err(),
        NfsError::Exists
    );
    assert_eq!(
        fs.setattr(
            &ctx,
            key,
            SetAttributes::default(),
            Some(NfsTime {
                seconds: 1,
                nanoseconds: 0
            })
        )
        .await
        .unwrap_err(),
        NfsError::NotSynchronized
    );
    let seen = fake.requests();
    assert_eq!(seen[0]["verifier"], "MTIzNDU2Nzg=");
    assert_eq!(seen[0]["exclusive"], true);
    assert_eq!(seen[2]["path"], "new");
    assert_eq!(seen[3]["size"], 0);
    assert_eq!(seen[3]["mode"], 0);
    assert_eq!(seen[5]["guard_modified_at"], "1970-01-01T00:00:01+00:00");
}

#[tokio::test]
async fn deletion_stales_handles_and_unsupported_operations_do_not_mutate() {
    let fake = Fake::new(vec![stat("file", false), response(json!({"ok":true}))]).await;
    let fs = fake.fs(false);
    let ctx = context();
    let key = fs
        .lookup(&ctx, fs.root(), &name("file"))
        .await
        .unwrap()
        .object;
    assert_eq!(
        fs.setattr(
            &ctx,
            key,
            SetAttributes {
                uid: Some(0),
                ..Default::default()
            },
            None
        )
        .await
        .unwrap_err(),
        NfsError::NotSupported
    );
    assert_eq!(
        fs.symlink(
            &ctx,
            fs.root(),
            &name("link"),
            b"file",
            SetAttributes::default()
        )
        .await
        .unwrap_err(),
        NfsError::NotSupported
    );
    fs.remove(&ctx, fs.root(), &name("file")).await.unwrap();
    assert_eq!(fs.getattr(&ctx, key).await.unwrap_err(), NfsError::Stale);
    assert_eq!(fake.requests().len(), 2);
    assert_eq!(fake.requests()[1]["directory"], false);
    let ro = fake.fs(true);
    assert_eq!(
        ro.create(
            &ctx,
            ro.root(),
            &name("blocked"),
            SetAttributes::default(),
            CreateMode::Unchecked
        )
        .await
        .unwrap_err(),
        NfsError::ReadOnly
    );
}

#[tokio::test]
async fn strict_response_validation_http_errors_and_no_write_retries() {
    for (status, body) in [
        (403, "{}"),
        (500, "{}"),
        (200, "{\"ok\":true,\"extra\":1}"),
        (200, "{\"ok\":true} trailing"),
        (200, "{}"),
        (200, "{\"ok\":false}"),
    ] {
        let fake = Fake::new(vec![(status, body.into(), Duration::ZERO)]).await;
        let fs = fake.fs(false);
        let expected = if status == 403 {
            NfsError::Access
        } else {
            NfsError::Io
        };
        assert_eq!(
            fs.getattr(&context(), fs.root()).await.unwrap_err(),
            expected
        );
        assert_eq!(fake.requests().len(), 1);
    }
    let fake = Fake::new(vec![
        stat("file", false),
        response(json!({"ok":true,"written":2})),
    ])
    .await;
    let fs = fake.fs(false);
    let key = fs
        .lookup(&context(), fs.root(), &name("file"))
        .await
        .unwrap()
        .object;
    assert_eq!(
        fs.write(&context(), key, 0, b"x", WriteStability::FileSync)
            .await
            .unwrap_err(),
        NfsError::Io
    );
    assert_eq!(fake.requests().len(), 2);
}

#[tokio::test]
async fn invalid_file_metadata_and_base64_never_become_success() {
    for entry in [
        json!({"path":"wrong","is_dir":false,"size":0,"mode":420,"modified_at":null}),
        json!({"path":"file","is_dir":false,"size":-1,"mode":420,"modified_at":null}),
        json!({"path":"file","is_dir":false,"size":0,"mode":4096,"modified_at":null}),
    ] {
        let fake = Fake::new(vec![response(json!({"ok":true,"entry":entry}))]).await;
        assert_eq!(
            fake.fs(false)
                .lookup(
                    &context(),
                    ObjectKey {
                        file_id: 1,
                        generation: 1
                    },
                    &name("file")
                )
                .await
                .unwrap_err(),
            NfsError::Io
        );
    }
    let fake = Fake::new(vec![
        stat("file", false),
        response(json!({"ok":true,"data":"%%%","eof":true})),
    ])
    .await;
    let fs = fake.fs(false);
    let key = fs
        .lookup(&context(), fs.root(), &name("file"))
        .await
        .unwrap()
        .object;
    assert_eq!(
        fs.read(&context(), key, 0, 1).await.unwrap_err(),
        NfsError::Io
    );
}

#[tokio::test]
async fn directory_continuations_refuse_changed_live_listing() {
    let fake = Fake::new(vec![
        response(json!({"ok":true,"entries":[entry("b",false),entry("a",false)]})),
        response(json!({"ok":true,"entries":[entry("c",false)]})),
    ])
    .await;
    let fs = fake.fs(false);
    let first = fs
        .readdir(&context(), fs.root(), 0, [0; 8], 100)
        .await
        .unwrap();
    assert_eq!(first.entries[0].name.as_bytes(), b"a");
    assert_eq!(
        fs.readdir(&context(), fs.root(), 1, first.verifier, 100)
            .await
            .unwrap_err(),
        NfsError::BadCookie
    );
}

#[tokio::test]
async fn size_bounds_cancellation_and_oversized_responses_fail_without_retry() {
    let fake = Fake::new(vec![
        stat("file", false),
        (
            200,
            json!({"ok":true,"written":1}).to_string(),
            Duration::from_secs(5),
        ),
    ])
    .await;
    let fs = Arc::new(fake.fs(false));
    let key = fs
        .lookup(&context(), fs.root(), &name("file"))
        .await
        .unwrap()
        .object;
    assert_eq!(
        fs.write(
            &context(),
            key,
            2_147_483_647,
            b"x",
            WriteStability::FileSync
        )
        .await
        .unwrap_err(),
        NfsError::FileTooLarge
    );
    let worker = fs.clone();
    let task = tokio::spawn(async move {
        worker
            .write(&context(), key, 0, b"x", WriteStability::FileSync)
            .await
    });
    fake.wait_count(2).await;
    assert_eq!(fake.requests().len(), 2);
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    tokio::time::sleep(Duration::from_millis(20)).await;
    assert_eq!(fake.requests().len(), 2);
    let oversized = Fake::new(vec![(200, " ".repeat(2 * 1024 * 1024 + 1), Duration::ZERO)]).await;
    assert_eq!(
        oversized
            .fs(false)
            .getattr(
                &context(),
                ObjectKey {
                    file_id: 1,
                    generation: 1
                }
            )
            .await
            .unwrap_err(),
        NfsError::Io
    );
}

#[tokio::test]
async fn creation_attributes_are_one_atomic_gateway_mutation() {
    let fake = Fake::new(vec![stat("file", false)]).await;
    let fs = fake.fs(false);
    fs.create(
        &context(),
        fs.root(),
        &name("file"),
        SetAttributes {
            size: Some(0),
            mode: Some(0o600),
            modify_time: Some(SetTime::ClientTime(NfsTime {
                seconds: 1,
                nanoseconds: 0,
            })),
            ..Default::default()
        },
        CreateMode::Unchecked,
    )
    .await
    .unwrap();
    let seen = fake.requests();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0]["size"], 0);
    assert_eq!(seen[0]["mode"], 384);
    assert_eq!(seen[0]["exclusive"], false);
    assert_eq!(seen[0]["modified_at"], "1970-01-01T00:00:01+00:00");
}

#[tokio::test]
async fn directory_verifier_distinguishes_component_boundaries() {
    let fake = Fake::new(vec![
        response(json!({"ok":true,"entries":[entry("a",false),entry("bc",false)]})),
        response(json!({"ok":true,"entries":[entry("ab",false),entry("c",false)]})),
    ])
    .await;
    let fs = fake.fs(false);
    let first = fs
        .readdir(&context(), fs.root(), 0, [0; 8], 100)
        .await
        .unwrap();
    assert_eq!(
        fs.readdir(&context(), fs.root(), 1, first.verifier, 100)
            .await
            .unwrap_err(),
        NfsError::BadCookie
    );
}

#[tokio::test]
async fn interrupted_rename_never_reuses_destination_handle() {
    let fake = Fake::new(vec![
        stat("old", false),
        stat("destination", false),
        (200, json!({"ok":true}).to_string(), Duration::MAX),
    ])
    .await;
    let fs = Arc::new(fake.fs(false));
    let old = fs
        .lookup(&context(), fs.root(), &name("old"))
        .await
        .unwrap()
        .object;
    let destination = fs
        .lookup(&context(), fs.root(), &name("destination"))
        .await
        .unwrap()
        .object;
    let worker = fs.clone();
    let task = tokio::spawn(async move {
        worker
            .rename(
                &context(),
                worker.root(),
                &name("old"),
                worker.root(),
                &name("destination"),
            )
            .await
    });
    fake.wait_count(3).await;
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    assert_eq!(
        fs.getattr(&context(), old).await.unwrap_err(),
        NfsError::Stale
    );
    assert_eq!(
        fs.getattr(&context(), destination).await.unwrap_err(),
        NfsError::Stale
    );
    assert_eq!(fake.requests().len(), 3);
}
