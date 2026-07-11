//! Check the read-only serve staleness detector against a disposable data dir.
//! Usage: fable_staleness <data_dir>

use std::path::PathBuf;

use engraph::config::Config;
use engraph::serve::read_only_index_staleness;
use engraph::store::Store;

fn main() -> anyhow::Result<()> {
    let data_dir = PathBuf::from(std::env::args().nth(1).expect("data_dir argument"));
    let store = Store::open(&data_dir.join("engraph.db"))?;
    let vault_path = PathBuf::from(
        store
            .get_meta("vault_path")?
            .ok_or_else(|| anyhow::anyhow!("lab database has no vault_path"))?,
    );
    let config = Config::default();

    match read_only_index_staleness(&store, &vault_path, &config)? {
        Some((latest_indexed_at, newest_vault_mtime)) => println!(
            "{{\"stale\":true,\"latest_indexed_at\":{latest_indexed_at},\"newest_vault_mtime\":{newest_vault_mtime}}}"
        ),
        None => println!("{{\"stale\":false}}"),
    }
    Ok(())
}
