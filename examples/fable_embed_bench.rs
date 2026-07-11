//! Micro-bench: split embed_text into phases — tokenize, context creation,
//! encode, extract — to attribute where embedding time goes.
//! Mirrors LlamaEmbed::embed_text (llm.rs) exactly, including n_ubatch=512.
//! Also benches a reused-context variant to measure the ceiling.
//!
//! Usage: fable_embed_bench <models_dir> <repeats> "text..."

use std::path::PathBuf;
use std::time::Instant;

use engraph::llm::llama_backend;
use llama_cpp_2::context::params::LlamaContextParams;
use llama_cpp_2::llama_batch::LlamaBatch;
use llama_cpp_2::model::params::LlamaModelParams;
use llama_cpp_2::model::{AddBos, LlamaModel};

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let models_dir = PathBuf::from(&args[1]);
    let repeats: usize = args[2].parse()?;
    let text = format!("task: search result | query: {}", &args[3]);

    let model_path =
        models_dir.join("ggml-org--embeddinggemma-300M-GGUF--embeddinggemma-300M-Q8_0.gguf");
    let backend = llama_backend()?;
    let t = Instant::now();
    let n_gpu: u32 = std::env::var("FABLE_N_GPU_LAYERS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(999);
    let model_params = LlamaModelParams::default().with_n_gpu_layers(n_gpu);
    let model = LlamaModel::load_from_file(backend, &model_path, &model_params)
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    eprintln!("model_load_us={}", t.elapsed().as_micros());

    let tokens = model
        .str_to_token(&text, AddBos::Never)
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    let n_tokens = tokens.len() as u32;
    eprintln!("n_tokens={n_tokens}");

    let sleep_ms: u64 = std::env::var("FABLE_SLEEP_MS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    // Phase-split runs, fresh context each time (current engraph behavior)
    for rep in 0..repeats {
        if sleep_ms > 0 {
            std::thread::sleep(std::time::Duration::from_millis(sleep_ms));
        }
        let t = Instant::now();
        let ctx_params = LlamaContextParams::default()
            .with_embeddings(true)
            .with_n_ctx(std::num::NonZeroU32::new(n_tokens.max(64) + 16))
            .with_n_ubatch(n_tokens.max(512))
            .with_n_batch(n_tokens.max(512));
        let mut ctx = model
            .new_context(backend, ctx_params)
            .map_err(|e| anyhow::anyhow!("{e}"))?;
        let t_ctx = t.elapsed().as_micros();

        let t = Instant::now();
        let mut batch = LlamaBatch::new(tokens.len() + 16, 1);
        batch
            .add_sequence(&tokens, 0, true)
            .map_err(|e| anyhow::anyhow!("{e}"))?;
        ctx.encode(&mut batch).map_err(|e| anyhow::anyhow!("{e}"))?;
        let t_encode = t.elapsed().as_micros();

        let t = Instant::now();
        let emb = ctx
            .embeddings_seq_ith(0)
            .map_err(|e| anyhow::anyhow!("{e}"))?;
        let dim = emb.len();
        let t_extract = t.elapsed().as_micros();

        let t = Instant::now();
        drop(ctx);
        let t_drop = t.elapsed().as_micros();
        println!(
            "{{\"variant\":\"fresh_ctx\",\"rep\":{rep},\"ctx_create_us\":{t_ctx},\"encode_us\":{t_encode},\"extract_us\":{t_extract},\"drop_us\":{t_drop},\"dim\":{dim}}}"
        );
    }

    // Reused-context variant: create once, encode repeatedly (clears KV each time)
    let ctx_params = LlamaContextParams::default()
        .with_embeddings(true)
        .with_n_ctx(std::num::NonZeroU32::new(512))
        .with_n_ubatch(512)
        .with_n_batch(512);
    let mut ctx = model
        .new_context(backend, ctx_params)
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    for rep in 0..repeats {
        let t = Instant::now();
        let mut batch = LlamaBatch::new(tokens.len() + 16, 1);
        batch
            .add_sequence(&tokens, 0, true)
            .map_err(|e| anyhow::anyhow!("{e}"))?;
        ctx.clear_kv_cache();
        ctx.encode(&mut batch).map_err(|e| anyhow::anyhow!("{e}"))?;
        let emb = ctx
            .embeddings_seq_ith(0)
            .map_err(|e| anyhow::anyhow!("{e}"))?;
        let dim = emb.len();
        let t_total = t.elapsed().as_micros();
        println!(
            "{{\"variant\":\"reused_ctx\",\"rep\":{rep},\"encode_total_us\":{t_total},\"dim\":{dim}}}"
        );
    }

    Ok(())
}
