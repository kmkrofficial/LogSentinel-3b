import os
import gc
import random
import numpy as np
import torch
import time
from tqdm import tqdm
from sklearn.metrics import accuracy_score, precision_recall_fscore_support, confusion_matrix
import bitsandbytes as bnb

from config import REPORTS_DIR, DEFAULT_BERT_PATH
from utils.database_manager import DatabaseManager
from utils.data_loader import LogDataset
from utils.resource_monitor import ResourceMonitor
from utils.log_visualizer import LogVisualizer
from logsentinel_model import LogSentinelModel

class TrainingController:
    def __init__(self, model_name, dataset_name, hyperparameters, db_manager, callback=None):
        self.model_name, self.dataset_name = model_name, dataset_name
        self.hp = hyperparameters
        self.db = db_manager
        self.callback = callback or (lambda *args: 'CONTINUE')
        self.run_id, self.model, self.train_dataset, self.test_dataset = None, None, None, None
        self.visualizer = None
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.global_step_count, self.total_training_steps, self.run_start_time = 0, 0, 0
        self.batch_losses = [] # For plotting loss curve

    def _log(self, message):
        print(message)
        self.callback({"log": message})

    def _initialize_run(self):
        self.run_id = self.db.create_new_run('Training', self.model_name, self.dataset_name, self.hp)
        if self.run_id:
            self._log(f"Created new training run with ID: {self.run_id}")
            self.report_dir = REPORTS_DIR / self.run_id
            self.report_dir.mkdir(exist_ok=True)
            self.visualizer = LogVisualizer(plot_dir=self.report_dir)
        return self.run_id is not None

    def _cleanup(self):
        self._log("Cleaning up training resources...")
        del self.model, self.train_dataset, self.test_dataset, self.visualizer
        self.model, self.train_dataset, self.test_dataset, self.visualizer = None, None, None, None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        self._log("Cleanup complete.")

    def _train_phase(self, phase_name, n_epochs, lr, indexes_for_phase):
        if not n_epochs > 0:
            return True
        self._log(f"\n--- Starting Training Phase: {phase_name} ---")
        self.model.train()
        criterion = torch.nn.CrossEntropyLoss()
        trainable_params = [p for p in self.model.parameters() if p.requires_grad]
        if not trainable_params:
            self._log(f"Phase '{phase_name}' skipped: No trainable parameters.")
            return True

        # --- FIX: Use PagedAdamW8bit for numerical stability with quantized models ---
        optimizer = bnb.optim.PagedAdamW8bit(trainable_params, lr=lr)

        model_dtype = next(self.model.parameters()).dtype
        use_scaler = (model_dtype == torch.float16)
        
        self._log(f"Model DType: {model_dtype}. Using GradScaler: {use_scaler}.")
        
        scaler = torch.cuda.amp.GradScaler(enabled=use_scaler)
        autocast_dtype = model_dtype if model_dtype in [torch.float16, torch.bfloat16] else torch.float16
        grad_accum_steps = self.hp['batch_size'] // self.hp['micro_batch_size']

        for epoch in range(int(n_epochs)):
            epoch_str = f"Epoch {epoch + 1}/{int(n_epochs)} ({phase_name})"
            self._log(f"--- {epoch_str} ---")
            random.shuffle(indexes_for_phase)
            optimizer.zero_grad()
            for i_th, start_idx in enumerate(range(0, len(indexes_for_phase), self.hp['micro_batch_size'])):
                self.global_step_count += 1
                end_idx = min(start_idx + self.hp['micro_batch_size'], len(indexes_for_phase))
                if not (batch_indexes := indexes_for_phase[start_idx:end_idx]):
                    continue
                seqs, labels = self.train_dataset.get_batch(batch_indexes)

                with torch.autocast(device_type=self.device.type, dtype=autocast_dtype, enabled=(self.device.type == 'cuda')):
                    logits, int_labels = self.model.train_helper(seqs, labels)
                    if logits.shape[0] == 0:
                        continue
                    loss = criterion(logits, int_labels) / grad_accum_steps
                
                scaler.scale(loss).backward()

                if (i_th + 1) % grad_accum_steps == 0:
                    scaler.unscale_(optimizer)
                    torch.nn.utils.clip_grad_norm_(trainable_params, max_norm=1.0)
                    scaler.step(optimizer)
                    scaler.update()
                    optimizer.zero_grad()
                
                current_loss = loss.item() * grad_accum_steps
                self.batch_losses.append(current_loss)
                elapsed = time.time() - self.run_start_time
                progress = self.global_step_count / self.total_training_steps if self.total_training_steps > 0 else 0
                etc = (elapsed / progress) * (1 - progress) if progress > 0 else 0
                status = {"epoch": epoch_str, "progress": progress, "loss": current_loss, "etc": etc}
                if self.callback(status) == 'STOP':
                    self._log("Stop request received. Aborting training.")
                    return False
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return True

    def _evaluate(self):
        self._log("\n--- Starting Final Evaluation ---")
        self.model.eval()
        all_preds, gt_labels = [], self.test_dataset.get_all_labels()
        eval_start_time = time.time()
        
        model_dtype = next(self.model.parameters()).dtype
        autocast_dtype = model_dtype if model_dtype in [torch.float16, torch.bfloat16] else torch.float16
        
        total_items = len(self.test_dataset)
        with torch.no_grad():
            for i in tqdm(range(0, total_items, self.hp['batch_size']), desc="Evaluating"):
                # --- FIX: Send progress updates during evaluation ---
                progress = (i + 1) / total_items
                self.callback({"epoch": "Final Evaluation", "progress": progress})

                end_idx = min(i + self.hp['batch_size'], total_items)
                seqs, _ = self.test_dataset.get_batch(list(range(i, end_idx)))
                with torch.autocast(device_type=self.device.type, dtype=autocast_dtype, enabled=(self.device.type == 'cuda')):
                    logits = self.model(seqs)
                all_preds.extend(torch.argmax(logits, dim=-1).cpu().numpy())
        
        total_eval_time = time.time() - eval_start_time
        valid_indices = (np.array(all_preds) != -1)
        preds_numeric, gt_numeric = np.array(all_preds)[valid_indices], gt_labels[valid_indices]
        
        p, r, f1, _ = precision_recall_fscore_support(gt_numeric, preds_numeric, average='binary', pos_label=1, zero_division=0)
        p_det, r_det, f1_det, s_det = precision_recall_fscore_support(gt_numeric, preds_numeric, labels=[0, 1], zero_division=0)
        time_per_record_ms = (total_eval_time / len(gt_numeric)) * 1000 if len(gt_numeric) > 0 else 0
        
        perf_metrics = {
            "overall": {
                "accuracy": accuracy_score(gt_numeric, preds_numeric), "precision": p, "recall": r, "f1_score": f1,
                "time_per_record_ms": time_per_record_ms
            },
            "per_class": {
                "normal": {"precision": p_det[0], "recall": r_det[0], "f1": f1_det[0], "support": int(s_det[0])},
                "anomalous": {"precision": p_det[1], "recall": r_det[1], "f1": f1_det[1], "support": int(s_det[1])}
            },
            "confusion_matrix": confusion_matrix(gt_numeric, preds_numeric, labels=[0, 1]).tolist(),
            "training_loss_series": self.batch_losses
        }
        
        self.visualizer.plot_confusion_matrix(np.array(perf_metrics['confusion_matrix']), ['Normal', 'Anomalous'])
        metrics_for_plot = {k: v for k, v in perf_metrics['overall'].items() if k in ['accuracy', 'precision', 'recall', 'f1_score']}
        self.visualizer.plot_overall_metrics(metrics_for_plot)
        self.visualizer.plot_training_loss(self.batch_losses)
        
        return perf_metrics

    def run(self):
        monitor = ResourceMonitor()
        monitor.start()
        final_status = 'FAILED'
        
        try:
            if not self._initialize_run():
                raise RuntimeError("Failed to create a new run record in the database.")
            
            self.run_start_time = time.time()
            self.train_dataset = LogDataset(os.path.join("datasets", self.dataset_name, 'train.csv'))
            self.test_dataset = LogDataset(os.path.join("datasets", self.dataset_name, 'test.csv'))
            
            base_indexes = list(range(len(self.train_dataset)))
            if self.train_dataset.num_less > 0 and self.train_dataset.num_less / len(self.train_dataset) < self.hp.get('min_less_portion', 0.5):
                add_num = int((self.hp['min_less_portion'] * self.train_dataset.num_majority) / (1 - self.hp['min_less_portion'])) - self.train_dataset.num_less
                if add_num > 0:
                    self._log(f"Oversampling minority class with {add_num} samples.")
                    base_indexes.extend(np.random.choice(self.train_dataset.less_indexes, add_num, replace=True).tolist())

            batches_per_epoch = len(base_indexes) // self.hp['micro_batch_size']
            self.total_training_steps = sum([self.hp.get(f'n_epochs_phase{i+1}', 0) * batches_per_epoch for i in range(4)])
            self._log(f"Total training steps calculated: {self.total_training_steps}")
            
            self.model = LogSentinelModel(DEFAULT_BERT_PATH, self.model_name, None, True, self.device, self.hp['max_content_len'], self.hp['max_seq_len'])
            
            training_phases = [
                ("Projector", self.model.set_train_only_projector, self.hp['n_epochs_phase1'], self.hp['lr_phase1']),
                ("Classifier", self.model.set_train_only_classifier, self.hp['n_epochs_phase2'], self.hp['lr_phase2']),
                ("Projector+Classifier", self.model.set_train_projector_and_classifier, self.hp['n_epochs_phase3'], self.hp['lr_phase3']),
                ("Fine-tuning All", self.model.set_finetuning_all, self.hp['n_epochs_phase4'], self.hp['lr_phase4'])
            ]
            
            all_phases_completed = True
            for name, setup_func, epochs, lr in training_phases:
                setup_func()
                if not self._train_phase(name, epochs, lr, base_indexes):
                    all_phases_completed = False
                    final_status = 'ABORTED'
                    break
            
            if all_phases_completed:
                perf_metrics = self._evaluate()
                perf_metrics['overall']['total_run_time_sec'] = time.time() - self.run_start_time
                self.db.save_performance_metrics(self.run_id, perf_metrics)
                
                final_model_path = self.report_dir / 'final_model'
                self.model.save_ft_model(str(final_model_path))
                final_status = 'COMPLETED'
        
        except Exception as e:
            error_msg = f"CRITICAL ERROR in run {self.run_id}: {e}"
            self._log(error_msg)
            self.callback({"error": str(e)})
            final_status = 'FAILED'
        
        finally:
            resource_metrics = monitor.stop()
            if self.run_id:
                self.db.save_resource_metrics(self.run_id, resource_metrics)
                if self.visualizer:
                    self.visualizer.plot_resource_usage(resource_metrics)
                self.db.update_run_status(self.run_id, final_status, str(self.report_dir) if final_status == 'COMPLETED' else None)
                self._log(f"Run {self.run_id} finished with status: {final_status}")
            
            self._cleanup()
            self.callback({"status": final_status, "done": True})