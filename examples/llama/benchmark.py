# SPDX-FileCopyrightText: Copyright (c) 2022-2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import csv
import json
from pathlib import Path

import numpy as np
import torch
from transformers import LlamaTokenizer

import tensorrt_llm
from tensorrt_llm.quantization import QuantMode
from tensorrt_llm.runtime import ModelConfig, SamplingConfig
from utils import fetch_inputs
import time
import wandb
from build import get_engine_name  # isort:skip

EOS_TOKEN = 2
PAD_TOKEN = 2


def throttle_generator(generator, stream_interval):
    for i, out in enumerate(generator):
        if not i % stream_interval:
            yield out

    if i % stream_interval:
        yield out


def read_config(config_path: Path):
    with open(config_path, 'r') as f:
        config = json.load(f)
    use_gpt_attention_plugin = config['plugin_config']['gpt_attention_plugin']
    remove_input_padding = config['plugin_config']['remove_input_padding']
    dtype = config['builder_config']['precision']
    tp_size = config['builder_config']['tensor_parallel']
    pp_size = config['builder_config']['pipeline_parallel']
    world_size = tp_size * pp_size
    assert world_size == tensorrt_llm.mpi_world_size(), \
        f'Engine world size ({world_size}) != Runtime world size ({tensorrt_llm.mpi_world_size()})'
    num_heads = config['builder_config']['num_heads'] // tp_size
    hidden_size = config['builder_config']['hidden_size'] // tp_size
    vocab_size = config['builder_config']['vocab_size']
    num_layers = config['builder_config']['num_layers']
    num_kv_heads = config['builder_config'].get('num_kv_heads', num_heads)
    paged_kv_cache = config['plugin_config']['paged_kv_cache']
    tokens_per_block = config['plugin_config']['tokens_per_block']
    quant_mode = QuantMode(config['builder_config']['quant_mode'])
    if config['builder_config'].get('multi_query_mode', False):
        tensorrt_llm.logger.warning(
            "`multi_query_mode` config is deprecated. Please rebuild the engine."
        )
        num_kv_heads = 1
    num_kv_heads = (num_kv_heads + tp_size - 1) // tp_size
    use_custom_all_reduce = config['plugin_config'].get('use_custom_all_reduce',
                                                        False)

    model_config = ModelConfig(num_heads=num_heads,
                               num_kv_heads=num_kv_heads,
                               hidden_size=hidden_size,
                               vocab_size=vocab_size,
                               num_layers=num_layers,
                               gpt_attention_plugin=use_gpt_attention_plugin,
                               paged_kv_cache=paged_kv_cache,
                               tokens_per_block=tokens_per_block,
                               remove_input_padding=remove_input_padding,
                               dtype=dtype,
                               quant_mode=quant_mode,
                               use_custom_all_reduce=use_custom_all_reduce)

    return model_config, tp_size, pp_size, dtype


def parse_input(input_text: str, input_file: str, tokenizer, end_id: int,
                remove_input_padding: bool):
    input_tokens = []
    if input_file is None:
        input_tokens.append(
            tokenizer.encode(input_text, add_special_tokens=False))
    else:
        if input_file.endswith('.csv'):
            with open(input_file, 'r') as csv_file:
                csv_reader = csv.reader(csv_file, delimiter=',')
                for line in csv_reader:
                    input_tokens.append(np.array(line, dtype='int32'))
        elif input_file.endswith('.npy'):
            inputs = np.load(input_file)
            for row in inputs:
                row = row[row != end_id]
                input_tokens.append(row)
        else:
            print('Input file format not supported.')
            raise SystemExit

    input_ids = None
    input_lengths = torch.tensor([len(x) for x in input_tokens],
                                 dtype=torch.int32,
                                 device='cuda')
    if remove_input_padding:
        input_ids = np.concatenate(input_tokens)
        input_ids = torch.tensor(input_ids, dtype=torch.int32,
                                 device='cuda').unsqueeze(0)
    else:
        input_ids = torch.nested.to_padded_tensor(
            torch.nested.nested_tensor(input_tokens, dtype=torch.int32),
            end_id).cuda()

    return input_ids, input_lengths


def prepare_inputs(input_texts,tokenizer):

    input_tokens = tokenizer.batch_encode_plus(input_texts,add_special_tokens=False, padding=True)
    #input_tokens = input_tokens['input_ids'].to(torch.cuda.current_device())
    input_tokens = torch.tensor(input_tokens['input_ids'],dtype=torch.int32,device="cuda")
    input_lengths = [it.shape[-1] for it in input_tokens]
    return input_tokens,torch.tensor(input_lengths,dtype=torch.int32,device="cuda")


def print_output(output_ids, input_lengths, max_output_len, tokenizer,
                 output_csv, output_npy):
    num_beams = output_ids.size(1)
    if output_csv is None and output_npy is None:
        for b in range(input_lengths.size(0)):
            inputs = output_ids[b][0][:input_lengths[b]].tolist()
            input_text = tokenizer.decode(inputs)
            print(f'Input: \"{input_text}\"')
            for beam in range(num_beams):
                output_begin = input_lengths[b]
                output_end = input_lengths[b] + max_output_len
                outputs = output_ids[b][beam][output_begin:output_end].tolist()
                output_text = tokenizer.decode(outputs)
                print(f'Output: \"{output_text}\"')

    output_ids = output_ids.reshape((-1, output_ids.size(2)))

    if output_csv is not None:
        output_file = Path(output_csv)
        output_file.parent.mkdir(exist_ok=True, parents=True)
        outputs = output_ids.tolist()
        with open(output_file, 'w') as csv_file:
            writer = csv.writer(csv_file, delimiter=',')
            writer.writerows(outputs)

    if output_npy is not None:
        output_file = Path(output_npy)
        output_file.parent.mkdir(exist_ok=True, parents=True)
        outputs = np.array(output_ids.cpu().contiguous(), dtype='int32')
        np.save(output_file, outputs)


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch_size', type=int, required=True)
    parser.add_argument('--max_output_len', type=int, required=True)
    parser.add_argument('--log_level', type=str, default='error')
    parser.add_argument('--engine_dir', type=str, default='llama_outputs')
    parser.add_argument('--tokenizer_dir',
                        type=str,
                        default=".",
                        help="Directory containing the tokenizer.model.")
    parser.add_argument(
        '--input_tokens',
        dest='input_file',
        type=str,
        help=
        'CSV or Numpy file containing tokenized input. Alternative to text input.',
        default=None)
    parser.add_argument('--output_csv',
                        type=str,
                        help='CSV file where the tokenized output is stored.',
                        default=None)
    parser.add_argument('--output_npy',
                        type=str,
                        help='Numpy file where the tokenized output is stored.',
                        default=None)
    parser.add_argument('--num_beams',
                        type=int,
                        help="Use beam search if num_beams >1",
                        default=1)
    parser.add_argument('--streaming', default=False, action='store_true')
    parser.add_argument('--streaming_interval',
                        type=int,
                        help="How often to return tokens when streaming.",
                        default=5)
    return parser.parse_args()


def generate(
    max_output_len: int,
    log_level: str = 'error',
    engine_dir: str = 'llama_outputs',
    input_text: str = 'Born in north-east France, Soyer trained as a',
    input_file: str = None,
    output_csv: str = None,
    output_npy: str = None,
    tokenizer_dir: str = None,
    num_beams: int = 1,
    streaming: bool = False,
    streaming_interval: int = 5,
):
    tensorrt_llm.logger.set_level(log_level)

    engine_dir = Path(engine_dir)
    config_path = engine_dir / 'config.json'
    model_config, tp_size, pp_size, dtype = read_config(config_path)
    world_size = tp_size * pp_size

    runtime_rank = tensorrt_llm.mpi_rank()
    runtime_mapping = tensorrt_llm.Mapping(world_size,
                                           runtime_rank,
                                           tp_size=tp_size,
                                           pp_size=pp_size)
    torch.cuda.set_device(runtime_rank % runtime_mapping.gpus_per_node)

    tokenizer = LlamaTokenizer.from_pretrained(tokenizer_dir, legacy=False)

    sampling_config = SamplingConfig(end_id=EOS_TOKEN,
                                     pad_id=PAD_TOKEN,
                                     num_beams=num_beams)

    engine_name = get_engine_name('llama', dtype, tp_size, pp_size,
                                  runtime_rank)
    serialize_path = engine_dir / engine_name
    with open(serialize_path, 'rb') as f:
        engine_buffer = f.read()
    decoder = tensorrt_llm.runtime.GenerationSession(model_config,
                                                     engine_buffer,
                                                     runtime_mapping,
                                                     debug_mode=False,
                                                     debug_tensors_to_save=None)
    if runtime_rank == 0:
        print(f"Running the {dtype} engine ...")


    input_ids, input_lengths = parse_input(input_text, input_file, tokenizer,
                                           EOS_TOKEN,
                                           model_config.remove_input_padding)

    max_input_length = torch.max(input_lengths).item()
    decoder.setup(input_lengths.size(0), max_input_length, max_output_len,
                  num_beams)

    output_gen_ids = decoder.decode(input_ids,
                                    input_lengths,
                                    sampling_config,
                                    streaming=streaming)
    torch.cuda.synchronize()
    if streaming:
        for output_ids in throttle_generator(output_gen_ids,
                                             streaming_interval):
            if runtime_rank == 0:
                print_output(output_ids, input_lengths, max_output_len,
                             tokenizer, output_csv, output_npy)
    else:
        output_ids = output_gen_ids
        if runtime_rank == 0:
            print_output(output_ids, input_lengths, max_output_len, tokenizer,
                         output_csv, output_npy)

def _generate(decoder,tokenizer,input_ids,input_lengths,sampling_config,max_output_len):

    output_lengths = []
    output_texts = []
    output_ids = decoder.decode(input_ids,
                                    input_lengths,
                                    sampling_config,
                                    streaming=False)
    #import pdb;pdb.set_trace()
    for b in range(input_lengths.shape[0]):
        output_begin = input_lengths[b]
        output_end = input_lengths[b] + max_output_len
        outputs = output_ids[b][0][output_begin:output_end].tolist()
        output_text = tokenizer.decode(outputs)
        output_texts.append(output_text)
        output_lengths.append(max_output_len)

    return output_texts, output_lengths


def run_benchmark_generate(
    batch_size: int,
    max_output_len: int,
    log_level: str = 'error',
    engine_dir: str = 'llama_outputs',
    input_file: str = None,
    output_csv: str = None,
    output_npy: str = None,
    tokenizer_dir: str = None,
    num_beams: int = 1,
    streaming: bool = False,
    streaming_interval: int = 5,
    cycles=3
):
    tensorrt_llm.logger.set_level(log_level)

    engine_dir = Path(engine_dir)
    config_path = engine_dir / 'config.json'
    model_config, tp_size, pp_size, dtype = read_config(config_path)
    world_size = tp_size * pp_size

    runtime_rank = tensorrt_llm.mpi_rank()
    runtime_mapping = tensorrt_llm.Mapping(world_size,
                                           runtime_rank,
                                           tp_size=tp_size,
                                           pp_size=pp_size)
    torch.cuda.set_device(runtime_rank % runtime_mapping.gpus_per_node)

    tokenizer = LlamaTokenizer.from_pretrained(tokenizer_dir, legacy=False)

    sampling_config = SamplingConfig(end_id=EOS_TOKEN,
                                     pad_id=PAD_TOKEN,
                                     num_beams=num_beams)

    engine_name = get_engine_name('llama', dtype, tp_size, pp_size,
                                  runtime_rank)
    serialize_path = engine_dir / engine_name
    with open(serialize_path, 'rb') as f:
        engine_buffer = f.read()
    decoder = tensorrt_llm.runtime.GenerationSession(model_config,
                                                     engine_buffer,
                                                     runtime_mapping,
                                                     debug_mode=False,
                                                     debug_tensors_to_save=None)
    if runtime_rank == 0:
        print(f"Running the {dtype} engine ...")
    
  
    
    input_texts,warmup_sentences = fetch_inputs(batch_size,tokenizer,per_seq_input_token_length=1000)

    print("*** Running warmup generate")
    t_generate_start = time.time()
    
    input_ids, input_lengths = prepare_inputs(warmup_sentences,tokenizer)

    max_input_length = torch.max(input_lengths).item()
    decoder.setup(input_lengths.size(0), max_input_length, max_output_len,
                 num_beams)

    output_texts,output_length = _generate(decoder,tokenizer,input_ids,input_lengths,sampling_config,max_output_len)

    print(warmup_sentences[0],output_texts[0])
    total_new_tokens = sum(output_length)
    t_generate_span = time.time() - t_generate_start
    print(f"{'-'*60}\n Time to generate: {t_generate_span}")
    torch.cuda.synchronize()
    
    print("*** Running benchmark")

    t0 = time.time()
    total_new_tokens_generated = 0

    for i in range(cycles):
        cycle_start = time.time()
        
        input_ids, input_lengths = prepare_inputs(input_texts,tokenizer)
        #import pdb;pdb.set_trace()
        max_input_length = torch.max(input_lengths).item()
        decoder.setup(input_lengths.size(0), max_input_length, max_output_len,
                  num_beams)
        output_texts,output_length = _generate(decoder,tokenizer,input_ids,input_lengths,sampling_config,max_output_len)
        total_new_tokens_generated += sum(output_length)
        cycle_time = time.time() - cycle_start
        print(f"cycle time:{cycle_time}")
    print(input_texts[0],output_texts[0])
    torch.cuda.synchronize()
    cycles_generation_time = time.time() - t0
    throughput_per_token = (cycles_generation_time) / (total_new_tokens_generated)
    throughput = (total_new_tokens_generated) / (cycles_generation_time)

    print(
        f"""
    *** Performance stats:
    Throughput per token including tokenize: {throughput_per_token*1000:.2f} msecs
    Throughput (tokens/sec):{throughput}
    Tokenize and generate {total_new_tokens_generated} (bs={batch_size}) tokens: {cycles_generation_time:.3f} secs
    """
    )
    
    return {
            "throughput_per_token": f"{throughput_per_token*1000:.2f} msecs",
            "total_new_tokens_generated": total_new_tokens_generated,
            "batch_size": batch_size,
            "throughput": f"{throughput} tokens/sec",
            "total_generation_time": t_generate_span,
        }



if __name__ == '__main__':
    args = parse_arguments()
    params = args.__dict__.copy()
    params['inference_engine'] = "tensorrt-llm"
    params['model_name'] = "glaiveai/glaive-coder-7b"
    wb = wandb.init(project="wizardcoder_batch_benchmark", config=params)
    result = run_benchmark_generate(**vars(args))
    wb.log(result)
