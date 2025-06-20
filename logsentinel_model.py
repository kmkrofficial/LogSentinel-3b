import torch
import os
from torch import nn
from peft import (
    PeftModel,
    LoraConfig,
    TaskType,
    get_peft_model,
    prepare_model_for_kbit_training
)
from transformers import BertTokenizerFast, BertModel
from utils.model_loader import load_model_and_tokenizer
from utils.helpers import merge_data, stack_and_pad_left

class LogSentinelModel(nn.Module):
    def __init__(self, bert_path, llama_path, ft_path=None, is_train_mode=True, device=None, max_content_len=128, max_seq_len=128):
        super().__init__()
        self.max_content_len = max_content_len
        self.max_seq_len = max_seq_len
        self.device = device or torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

        self.llama_model, self.llama_tokenizer = load_model_and_tokenizer(llama_path, is_train_mode)
        self.bert_tokenizer = BertTokenizerFast.from_pretrained(bert_path, do_lower_case=True)
        self.bert_model = BertModel.from_pretrained(
            bert_path, 
            low_cpu_mem_usage=True, 
            gradient_checkpointing=True
        ).to(self.device)

        projector_device = self.llama_model.device
        compute_dtype = self.llama_model.dtype
        bert_hidden_size = self.bert_model.config.hidden_size
        llama_hidden_size = self.llama_model.config.hidden_size

        self.projector = nn.Sequential(
            nn.Linear(bert_hidden_size, llama_hidden_size),
            nn.GELU(),
            nn.Linear(llama_hidden_size, llama_hidden_size)
        ).to(projector_device).to(compute_dtype)

        self.classifier = nn.Linear(llama_hidden_size, 2).to(projector_device).to(compute_dtype)

        self.instruc_tokens = self.llama_tokenizer(
            ['Below is a sequence of system log messages:'],
            return_tensors="pt", padding=True
        ).to(projector_device)
        
        self._setup_peft(ft_path, is_train_mode)

    def _setup_peft(self, ft_path, is_train_mode):
        if ft_path and os.path.exists(os.path.join(ft_path, 'adapter_config.json')):
            print(f"Loading components from fine-tuned path: {ft_path}")
            self.llama_model = PeftModel.from_pretrained(
                self.llama_model, 
                os.path.join(ft_path, 'Llama_ft'), 
                is_trainable=is_train_mode
            )
            self.projector.load_state_dict(torch.load(os.path.join(ft_path, 'projector.pt')))
            self.classifier.load_state_dict(torch.load(os.path.join(ft_path, 'classifier.pt')))
            print("PEFT adapter and other components loaded for inference/continued training.")
        
        elif is_train_mode:
            print("No adapter found. Creating new PEFT configuration for training.")
            if getattr(self.llama_model, "is_loaded_in_8bit", False) or getattr(self.llama_model, "is_loaded_in_4bit", False):
                self.llama_model = prepare_model_for_kbit_training(self.llama_model)
            
            lora_config = LoraConfig(
                r=8, lora_alpha=16, lora_dropout=0.1,
                target_modules=["q_proj", "v_proj"],
                bias="none", task_type=TaskType.CAUSAL_LM
            )
            self.llama_model = get_peft_model(self.llama_model, lora_config)
            self.llama_model.print_trainable_parameters()
        else:
            print("Warning: Inference mode selected but no fine-tuned adapter path was provided.")

    def save_ft_model(self, path):
        os.makedirs(path, exist_ok=True)
        self.llama_model.save_pretrained(os.path.join(path, 'Llama_ft'))
        torch.save(self.projector.state_dict(), os.path.join(path, 'projector.pt'))
        torch.save(self.classifier.state_dict(), os.path.join(path, 'classifier.pt'))
        print(f"Fine-tuned adapter and components saved to {path}")

    def _set_trainable(self, **kwargs):
        for name, param in self.named_parameters():
            param.requires_grad = False
            if 'projector' in name and kwargs.get('projector'): param.requires_grad = True
            elif 'classifier' in name and kwargs.get('classifier'): param.requires_grad = True
            elif 'llama_model' in name and 'lora_' in name and kwargs.get('llama_lora'): param.requires_grad = True

    def set_train_only_projector(self): self._set_trainable(projector=True)
    def set_train_only_classifier(self): self._set_trainable(classifier=True)
    def set_train_projector_and_classifier(self): self._set_trainable(projector=True, classifier=True)
    def set_finetuning_all(self): self._set_trainable(projector=True, classifier=True, llama_lora=True)

    def get_cls_embeddings(self, sequences_):
        sequences = [s[:self.max_seq_len] for s in sequences_]
        merged_logs, start_positions = merge_data(sequences)
        if not merged_logs: return None, None

        # --- FIX: Inner-loop batching for BERT processing ---
        all_bert_outputs = []
        physical_batch_size = 64 # Process 64 log lines at a time
        with torch.no_grad():
            for i in range(0, len(merged_logs), physical_batch_size):
                batch_logs = merged_logs[i:i+physical_batch_size]
                inputs = self.bert_tokenizer(batch_logs, return_tensors="pt", max_length=self.max_content_len, padding=True, truncation=True).to(self.bert_model.device)
                outputs = self.bert_model(**inputs).pooler_output
                all_bert_outputs.append(outputs.cpu())
        
        if not all_bert_outputs: return None, None
        bert_outputs = torch.cat(all_bert_outputs, dim=0).to(self.device)
        # --- END FIX ---

        projector_dtype = next(self.projector.parameters()).dtype
        projected_outputs = self.projector(bert_outputs.to(projector_dtype)).to(self.llama_model.dtype)
        if projected_outputs.shape[0] == 0: return [], self.llama_model.device
        split_indices = start_positions[1:]
        if not split_indices: return [projected_outputs], self.llama_model.device
        return list(torch.tensor_split(projected_outputs, split_indices, dim=0)), self.llama_model.device

    def _get_logits(self, sequences_):
        seq_embeddings, embed_device = self.get_cls_embeddings(sequences_)
        if seq_embeddings is None: return None, None
        embed_layer = self.llama_model.get_input_embeddings(); instruc_embeds = embed_layer(self.instruc_tokens['input_ids'])
        valid_embeddings, original_indices = [], []
        for i, seq_embed in enumerate(seq_embeddings):
            if seq_embed is not None and seq_embed.shape[0] > 0:
                full_embed = torch.cat([instruc_embeds[0], seq_embed], dim=0); valid_embeddings.append(full_embed); original_indices.append(i)
        if not valid_embeddings: return None, None
        inputs_embeds, attention_mask = stack_and_pad_left(valid_embeddings)
        outputs = self.llama_model(inputs_embeds=inputs_embeds, attention_mask=attention_mask, output_hidden_states=True)
        if hasattr(outputs, 'last_hidden_state'): last_hidden_state = outputs.last_hidden_state
        elif hasattr(outputs, 'hidden_states'): last_hidden_state = outputs.hidden_states[-1]
        else: raise AttributeError("Model output does not contain 'last_hidden_state' or 'hidden_states'.")
        sequence_lengths = attention_mask.sum(dim=1) - 1; batch_indices = torch.arange(len(valid_embeddings), device=last_hidden_state.device)
        cls_input_hidden_state = last_hidden_state[batch_indices, sequence_lengths]
        classifier_dtype = next(self.classifier.parameters()).dtype; logits = self.classifier(cls_input_hidden_state.to(classifier_dtype))
        return logits, original_indices

    def forward(self, sequences_):
        self.eval()
        with torch.inference_mode():
            logits, original_indices = self._get_logits(sequences_)
            if logits is None:
                return torch.full((len(sequences_), 2), -float('inf'), device=self.device)
            full_batch_logits = torch.full((len(sequences_), 2), -float('inf'), device=logits.device, dtype=logits.dtype)
            full_batch_logits[original_indices] = logits
            return full_batch_logits

    def train_helper(self, sequences_, labels):
        self.train()
        logits, original_indices = self._get_logits(sequences_)
        if logits is None: return torch.tensor([]), torch.tensor([])
        valid_str_labels = [labels[i] for i in original_indices]
        integer_labels = torch.tensor([1 if lbl == 'anomalous' else 0 for lbl in valid_str_labels], dtype=torch.long, device=logits.device)
        return logits, integer_labels