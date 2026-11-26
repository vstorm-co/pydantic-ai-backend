use crate::{
    handles::Handles,
    rpc::{self, Entry, Gateway, Request, CHUNK, MAX_FILE_BYTES},
};
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use nfsembed::vfs::*;
use std::{
    collections::hash_map::DefaultHasher,
    hash::{Hash, Hasher},
    sync::{Mutex, MutexGuard},
};

pub struct ProjectFs {
    gateway: Gateway,
    handles: Mutex<Handles>,
    mutations: tokio::sync::Mutex<()>,
    uid: u32,
    gid: u32,
    read_only: bool,
}
impl ProjectFs {
    pub fn new(gateway: Gateway, uid: u32, gid: u32, read_only: bool) -> Self {
        Self {
            gateway,
            handles: Mutex::new(Handles::default()),
            mutations: tokio::sync::Mutex::new(()),
            uid,
            gid,
            read_only,
        }
    }
    fn handles(&self) -> Result<MutexGuard<'_, Handles>, NfsError> {
        self.handles.lock().map_err(|_| NfsError::Io)
    }
    fn path(&self, object: ObjectKey) -> Result<String, NfsError> {
        self.handles()?.path(object)
    }
    fn writable(&self) -> Result<(), NfsError> {
        if self.read_only {
            Err(NfsError::ReadOnly)
        } else {
            Ok(())
        }
    }
    fn child(&self, parent: ObjectKey, name: &NfsName) -> Result<String, NfsError> {
        let name = std::str::from_utf8(name.as_bytes()).map_err(|_| NfsError::Invalid)?;
        if matches!(name, "." | "..") || name.contains('\\') {
            return Err(NfsError::Access);
        }
        let parent = self.path(parent)?;
        Ok(if parent == "." {
            name.into()
        } else {
            format!("{parent}/{name}")
        })
    }
    fn attributes(
        &self,
        entry: Entry,
        expected: &str,
        key: ObjectKey,
    ) -> Result<FileAttributes, NfsError> {
        if entry.path != expected || entry.mode > 0o777 {
            return Err(NfsError::Io);
        }
        let time = match entry.modified_at {
            Some(value) => {
                let parsed = DateTime::parse_from_rfc3339(&value).map_err(|_| NfsError::Io)?;
                NfsTime {
                    seconds: u64::try_from(parsed.timestamp()).map_err(|_| NfsError::Io)?,
                    nanoseconds: parsed.timestamp_subsec_nanos(),
                }
            }
            None => NfsTime::default(),
        };
        Ok(FileAttributes {
            file_type: if entry.is_dir {
                FileType::Directory
            } else {
                FileType::Regular
            },
            mode: entry.mode,
            links: if entry.is_dir { 2 } else { 1 },
            uid: self.uid,
            gid: self.gid,
            size: entry.size,
            used: entry.size,
            device: None,
            fs_id: 1,
            file_id: key.file_id,
            access_time: time,
            modify_time: time,
            change_time: time,
        })
    }
    async fn object_call(&self, request: &Request<'_>) -> Result<rpc::Response, NfsError> {
        match self.gateway.call(request).await {
            Err(NfsError::NotFound) => {
                if request.path != "." {
                    self.handles()?.remove_tree(request.path);
                }
                Err(NfsError::Stale)
            }
            result => result,
        }
    }
    async fn stat(&self, key: ObjectKey) -> Result<FileAttributes, NfsError> {
        let path = self.path(key)?;
        let result = self.object_call(&Request::new("stat", &path)).await?;
        self.attributes(result.entry.ok_or(NfsError::Io)?, &path, key)
    }
    async fn created(&self, request: &Request<'_>) -> Result<CreatedObject, NfsError> {
        let entry = self
            .gateway
            .call(request)
            .await?
            .entry
            .ok_or(NfsError::Io)?;
        if entry.path != request.path {
            return Err(NfsError::Io);
        }
        let key = self.handles()?.register(request.path)?;
        Ok(CreatedObject {
            object: key,
            attributes: Some(self.attributes(entry, request.path, key)?),
        })
    }
    async fn delete(
        &self,
        parent: ObjectKey,
        name: &NfsName,
        directory: bool,
    ) -> Result<MutationResult<()>, NfsError> {
        self.writable()?;
        let _mutation = self.mutations.lock().await;
        let path = self.child(parent, name)?;
        let mut request = Request::new("remove", &path);
        request.directory = Some(directory);
        // Unknown acceptance must not leave a handle that can alias a replacement.
        self.handles()?.remove_tree(&path);
        self.gateway.call(&request).await?;
        Ok(mutation(()))
    }
}
fn mutation<T>(value: T) -> MutationResult<T> {
    MutationResult {
        value,
        before: None,
        after: None,
    }
}
fn timestamp(value: NfsTime) -> Result<String, NfsError> {
    let seconds = i64::try_from(value.seconds).map_err(|_| NfsError::Invalid)?;
    DateTime::<Utc>::from_timestamp(seconds, value.nanoseconds)
        .map(|t| t.to_rfc3339())
        .ok_or(NfsError::Invalid)
}
fn time_value(value: SetTime) -> Result<String, NfsError> {
    match value {
        SetTime::ServerTime => Ok(Utc::now().to_rfc3339()),
        SetTime::ClientTime(time) => timestamp(time),
    }
}
fn attrs_request<'a>(
    op: &'a str,
    path: &'a str,
    attributes: SetAttributes,
    uid: u32,
    gid: u32,
) -> Result<Request<'a>, NfsError> {
    if attributes.uid.is_some_and(|v| v != uid) || attributes.gid.is_some_and(|v| v != gid) {
        return Err(NfsError::NotSupported);
    }
    if attributes.mode.is_some_and(|v| v > 0o777) {
        return Err(NfsError::NotSupported);
    }
    // The backing workspace has mtime only; do not claim to persist a distinct atime.
    if attributes.access_time.is_some() && attributes.access_time != attributes.modify_time {
        return Err(NfsError::NotSupported);
    }
    if attributes.size.is_some_and(|size| size > MAX_FILE_BYTES) {
        return Err(NfsError::FileTooLarge);
    }
    let mut request = Request::new(op, path);
    request.mode = attributes.mode;
    request.size = attributes.size;
    request.modified_at = attributes.modify_time.map(time_value).transpose()?;
    Ok(request)
}

#[async_trait]
impl VirtualFileSystem for ProjectFs {
    fn capabilities(&self) -> VfsCapabilities {
        VfsCapabilities {
            read_only: self.read_only,
            hard_links: false,
            symbolic_links: false,
            mknod: false,
            homogeneous: true,
            can_set_time: true,
        }
    }
    fn root(&self) -> ObjectKey {
        ObjectKey {
            file_id: 1,
            generation: 1,
        }
    }
    async fn getattr(
        &self,
        _: &RequestContext,
        object: ObjectKey,
    ) -> Result<FileAttributes, NfsError> {
        let _operation = self.mutations.lock().await;
        self.stat(object).await
    }
    async fn lookup(
        &self,
        _: &RequestContext,
        parent: ObjectKey,
        name: &NfsName,
    ) -> Result<CreatedObject, NfsError> {
        let _operation = self.mutations.lock().await;
        let path = self.child(parent, name)?;
        let entry = self
            .gateway
            .call(&Request::new("stat", &path))
            .await?
            .entry
            .ok_or(NfsError::Io)?;
        if entry.path != path {
            return Err(NfsError::Io);
        }
        let key = self.handles()?.register(&path)?;
        Ok(CreatedObject {
            object: key,
            attributes: Some(self.attributes(entry, &path, key)?),
        })
    }
    async fn access(
        &self,
        _: &RequestContext,
        object: ObjectKey,
        requested: u32,
    ) -> Result<u32, NfsError> {
        let _operation = self.mutations.lock().await;
        self.stat(object).await?;
        // Mode checks are advisory; every RPC is separately authorized by the gateway.
        Ok(if self.read_only {
            requested & 0x23
        } else {
            requested & 0x3f
        })
    }
    async fn setattr(
        &self,
        _: &RequestContext,
        object: ObjectKey,
        attributes: SetAttributes,
        guard: Option<NfsTime>,
    ) -> Result<MutationResult<()>, NfsError> {
        self.writable()?;
        let _mutation = self.mutations.lock().await;
        let path = self.path(object)?;
        let mut request = attrs_request("setattr", &path, attributes, self.uid, self.gid)?;
        request.guard_modified_at = guard.map(timestamp).transpose()?;
        self.object_call(&request).await?;
        Ok(mutation(()))
    }
    async fn read(
        &self,
        _: &RequestContext,
        object: ObjectKey,
        offset: u64,
        count: u32,
    ) -> Result<ReadResult, NfsError> {
        let _operation = self.mutations.lock().await;
        if offset > MAX_FILE_BYTES {
            return Err(NfsError::FileTooLarge);
        }
        if count > CHUNK {
            return Err(NfsError::Invalid);
        }
        let path = self.path(object)?;
        let mut request = Request::new("read", &path);
        request.offset = Some(offset);
        request.count = Some(count);
        let response = self.object_call(&request).await?;
        let data = rpc::decode(&response.data.ok_or(NfsError::Io)?)?;
        let eof = response.eof.ok_or(NfsError::Io)?;
        if data.len() > count as usize || (data.is_empty() && count > 0 && !eof) {
            return Err(NfsError::Io);
        }
        Ok(ReadResult {
            data,
            eof,
            attributes: None,
        })
    }
    async fn write(
        &self,
        _: &RequestContext,
        object: ObjectKey,
        offset: u64,
        data: &[u8],
        _: WriteStability,
    ) -> Result<MutationResult<WriteResult>, NfsError> {
        self.writable()?;
        if offset
            .checked_add(data.len() as u64)
            .is_none_or(|end| end > MAX_FILE_BYTES)
        {
            return Err(NfsError::FileTooLarge);
        }
        if data.len() > CHUNK as usize {
            return Err(NfsError::Invalid);
        }
        let _mutation = self.mutations.lock().await;
        let path = self.path(object)?;
        let mut request = Request::new("write", &path);
        request.offset = Some(offset);
        request.data = Some(rpc::encode(data));
        let written = self
            .gateway
            .call(&request)
            .await?
            .written
            .ok_or(NfsError::Io)?;
        if written as usize > data.len() || (written == 0 && !data.is_empty()) {
            return Err(NfsError::Io);
        }
        // HTTP success must mean the gateway transaction and backing provider flush committed.
        Ok(mutation(WriteResult {
            count: written,
            committed: WriteStability::FileSync,
        }))
    }
    async fn create(
        &self,
        _: &RequestContext,
        parent: ObjectKey,
        name: &NfsName,
        attributes: SetAttributes,
        mode: CreateMode,
    ) -> Result<MutationResult<CreatedObject>, NfsError> {
        self.writable()?;
        let _mutation = self.mutations.lock().await;
        let path = self.child(parent, name)?;
        let mut request = attrs_request("create", &path, attributes, self.uid, self.gid)?;
        request.exclusive = Some(!matches!(mode, CreateMode::Unchecked));
        if let CreateMode::Exclusive { verifier } = mode {
            request.verifier = Some(rpc::encode(&verifier));
        }
        request.mode = Some(request.mode.unwrap_or(0o666));
        Ok(mutation(self.created(&request).await?))
    }
    async fn mkdir(
        &self,
        _: &RequestContext,
        parent: ObjectKey,
        name: &NfsName,
        attributes: SetAttributes,
    ) -> Result<MutationResult<CreatedObject>, NfsError> {
        self.writable()?;
        let _mutation = self.mutations.lock().await;
        let path = self.child(parent, name)?;
        if attributes.size.is_some() {
            return Err(NfsError::NotSupported);
        }
        let mut request = attrs_request("mkdir", &path, attributes, self.uid, self.gid)?;
        request.mode = Some(request.mode.unwrap_or(0o777));
        Ok(mutation(self.created(&request).await?))
    }
    async fn remove(
        &self,
        _: &RequestContext,
        parent: ObjectKey,
        name: &NfsName,
    ) -> Result<MutationResult<()>, NfsError> {
        self.delete(parent, name, false).await
    }
    async fn rmdir(
        &self,
        _: &RequestContext,
        parent: ObjectKey,
        name: &NfsName,
    ) -> Result<MutationResult<()>, NfsError> {
        self.delete(parent, name, true).await
    }
    async fn rename(
        &self,
        _: &RequestContext,
        from_parent: ObjectKey,
        from_name: &NfsName,
        to_parent: ObjectKey,
        to_name: &NfsName,
    ) -> Result<(MutationResult<()>, MutationResult<()>), NfsError> {
        self.writable()?;
        let _mutation = self.mutations.lock().await;
        let source = self.child(from_parent, from_name)?;
        let destination = self.child(to_parent, to_name)?;
        if source != destination {
            let mut request = Request::new("rename", &source);
            request.destination = Some(&destination);
            // Tombstone before awaiting: cancellation may occur after the remote rename commits.
            let entries = {
                let mut handles = self.handles()?;
                let entries = handles.take_tree(&source);
                handles.remove_tree(&destination);
                entries
            };
            self.gateway.call(&request).await?;
            self.handles()?
                .finish_rename(entries, &source, &destination);
        } else {
            self.gateway.call(&Request::new("stat", &source)).await?;
        }
        Ok((mutation(()), mutation(())))
    }
    async fn readdir(
        &self,
        _: &RequestContext,
        directory: ObjectKey,
        cookie: u64,
        verifier: [u8; 8],
        _: usize,
    ) -> Result<ReadDirectoryPage, NfsError> {
        let _operation = self.mutations.lock().await;
        let path = self.path(directory)?;
        let mut entries = self
            .gateway
            .call(&Request::new("list", &path))
            .await?
            .entries
            .ok_or(NfsError::Io)?;
        entries.sort_by(|a, b| a.path.cmp(&b.path));
        // A content-derived verifier refuses continuation after the live directory changes.
        let mut hash = DefaultHasher::new();
        entries.len().hash(&mut hash);
        for entry in &entries {
            entry.path.hash(&mut hash);
        }
        let current = hash.finish().to_be_bytes();
        if cookie != 0 && verifier != current {
            return Err(NfsError::BadCookie);
        }
        let start = usize::try_from(cookie).map_err(|_| NfsError::BadCookie)?;
        if start > entries.len() {
            return Err(NfsError::BadCookie);
        }
        let prefix = if path == "." {
            String::new()
        } else {
            format!("{path}/")
        };
        let mut result = Vec::new();
        let mut previous = None;
        for (index, entry) in entries.into_iter().enumerate() {
            let name = entry.path.strip_prefix(&prefix).ok_or(NfsError::Io)?;
            if name.is_empty() || name.contains('/') || previous.as_deref() == Some(name) {
                return Err(NfsError::Io);
            }
            previous = Some(name.to_owned());
            let name = NfsName::new(name.as_bytes())?;
            let key = self.handles()?.register(&entry.path)?;
            let attributes = self.attributes(entry, &format!("{prefix}{name}"), key)?;
            if index >= start {
                result.push(DirectoryEntry {
                    object: key,
                    file_id: key.file_id,
                    name,
                    cookie: index as u64 + 1,
                    attributes: Some(attributes),
                });
            }
        }
        Ok(ReadDirectoryPage {
            verifier: current,
            entries: result,
            eof: true,
        })
    }
    async fn fsinfo(&self, _: &RequestContext, object: ObjectKey) -> Result<FsInfo, NfsError> {
        let _operation = self.mutations.lock().await;
        self.stat(object).await?;
        Ok(FsInfo {
            max_read: CHUNK,
            preferred_read: CHUNK,
            read_multiple: 1,
            max_write: CHUNK,
            preferred_write: CHUNK,
            write_multiple: 1,
            preferred_readdir: CHUNK,
            max_file_size: MAX_FILE_BYTES,
            time_granularity: NfsTime {
                seconds: 0,
                nanoseconds: 1,
            },
        })
    }
    async fn pathconf(&self, _: &RequestContext, object: ObjectKey) -> Result<PathConf, NfsError> {
        let _operation = self.mutations.lock().await;
        self.stat(object).await?;
        Ok(PathConf {
            max_links: 1,
            max_name_length: 255,
            no_truncation: true,
            chown_restricted: true,
            case_insensitive: false,
            case_preserving: true,
        })
    }
    async fn commit(
        &self,
        _: &RequestContext,
        object: ObjectKey,
        _: u64,
        _: u32,
    ) -> Result<MutationResult<()>, NfsError> {
        let _operation = self.mutations.lock().await;
        self.stat(object).await?;
        Ok(mutation(()))
    }
}
