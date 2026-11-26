use nfsembed::vfs::{NfsError, ObjectKey};
use std::collections::BTreeMap;

const MAX_HANDLES: usize = 16_384;
pub struct Handles {
    next: u64,
    paths: BTreeMap<u64, String>,
}
impl Default for Handles {
    fn default() -> Self {
        Self {
            next: 2,
            paths: BTreeMap::from([(1, ".".into())]),
        }
    }
}
impl Handles {
    pub fn path(&self, key: ObjectKey) -> Result<String, NfsError> {
        if key.generation != 1 {
            return Err(NfsError::Stale);
        }
        self.paths.get(&key.file_id).cloned().ok_or(NfsError::Stale)
    }
    pub fn register(&mut self, path: &str) -> Result<ObjectKey, NfsError> {
        if !valid_path(path) {
            return Err(NfsError::Invalid);
        }
        if let Some((id, _)) = self.paths.iter().find(|(_, p)| p.as_str() == path) {
            return Ok(ObjectKey {
                file_id: *id,
                generation: 1,
            });
        }
        if self.paths.len() >= MAX_HANDLES || self.next == u64::MAX {
            return Err(NfsError::NoSpace);
        }
        let id = self.next;
        self.next += 1;
        self.paths.insert(id, path.into());
        Ok(ObjectKey {
            file_id: id,
            generation: 1,
        })
    }
    pub fn remove_tree(&mut self, path: &str) {
        let prefix = format!("{path}/");
        self.paths
            .retain(|_, p| p != path && !p.starts_with(&prefix));
    }
    pub fn take_tree(&mut self, path: &str) -> Vec<(u64, String)> {
        let prefix = format!("{path}/");
        let taken = self
            .paths
            .iter()
            .filter(|(_, p)| *p == path || p.starts_with(&prefix))
            .map(|(id, path)| (*id, path.clone()))
            .collect::<Vec<_>>();
        self.remove_tree(path);
        taken
    }
    pub fn finish_rename(&mut self, entries: Vec<(u64, String)>, source: &str, destination: &str) {
        for (id, path) in entries {
            self.paths
                .insert(id, format!("{destination}{}", &path[source.len()..]));
        }
    }
}
pub fn valid_path(path: &str) -> bool {
    path == "."
        || (!path.is_empty()
            && path.len() <= 4096
            && !path.contains(['\\', '\0'])
            && path
                .split('/')
                .all(|p| !p.is_empty() && p != "." && p != ".."))
}
