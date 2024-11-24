""" 
Run Riesz Representer ATE estimation based causal effect regularized 
sentiment analysis. CausalSent. 
"""

import os
import sys
TOP_DIR = os.path.abspath(os.path.join(os.getcwd(), '..'))
if TOP_DIR not in sys.path:
    sys.path.insert(0, TOP_DIR)
import torch
from torch.utils.data import DataLoader
from sklearn.metrics import accuracy_score, f1_score
from causalign.data.utils import load_imdb_data, load_civil_commments_data
from causalign.data.generators import IMDBDataset, CivilCommentsDataset
from causalign.modules.causal_sent import CausalSent
from causalign.data.generators import SimilarityDataset
from causalign.utils import save_model, get_default_training_args, seed_everything  # TODO: use save_model
import wandb



def train_causal_sent(args):
    """ 
    Dataset preparation and training loop for the CausalSent model.
    """
    seed_everything(args.seed)
    
    # ====== Verbose Argument Printout ======
    print("\n" + "="*50)
    print("Running CausalSent Training with the following arguments:")
    for arg, value in vars(args).items():
        print(f"{arg}: {value}")
    print("="*50 + "\n")
    
    # =========== Load Data ==============
    if args.dataset == "imdb":
        imdb_train = load_imdb_data(split = "train")
        imdb_val = load_imdb_data(split = "test")

        imdb_ds_train: IMDBDataset = IMDBDataset(imdb_train, 
                                        split="train",
                                        args=args)
        imdb_ds_val: IMDBDataset = IMDBDataset(imdb_val,
                                            split = "validation", 
                                            args = args)
        ds_train = imdb_ds_train
        ds_val = imdb_ds_val
    else: 
        civil_train = load_civil_commments_data(split = "train")
        civil_val = load_civil_commments_data(split = "test")
        
        civil_ds_train: CivilCommentsDataset = CivilCommentsDataset(civil_train, 
                                                    split="train",
                                                    args=args)
        civil_ds_val: CivilCommentsDataset = CivilCommentsDataset(civil_val,
                                                    split = "validation", 
                                                    args = args)
        ds_train = civil_ds_train
        ds_val = civil_ds_val
    
    # ======= Setup Tracking and Device ========
    # Initialize wandb
    wandb.init(project="causal-sentiment", config=args)
    # Device setup
    device = torch.device("cuda" if torch.cuda.is_available() 
                        else "mps" if torch.backends.mps.is_available() 
                        else "cpu")
    print(f"Using device: {device}")

    # ======== Setup Training ==========
    # Hyperparameters
    lambda_bce: float = args.lambda_bce
    lambda_reg: float = args.lambda_reg
    lambda_riesz: float = args.lambda_riesz
    batch_size: int = args.batch_size
    epochs: int = args.epochs
    log_every: int = args.log_every
    running_ate: bool = args.running_ate # whether to track a running average or batch average to compute the RR ATE
    pretrained_model_name: str = args.pretrained_model_name
    lr: float = args.lr
    estimate_targets_for_ate: bool = args.estimate_targets_for_ate # whether to use estimated sentiment probabilities or true targets to compute the RR ATE

    # DataLoaders
    train_loader = DataLoader(ds_train, batch_size=batch_size, shuffle=True, collate_fn=SimilarityDataset.collate_fn)
    val_loader = DataLoader(ds_val, batch_size=batch_size, collate_fn=SimilarityDataset.collate_fn)

    # Model, optimizer, and loss
    model = CausalSent(bert_hidden_size=768, pretrained_model_name=pretrained_model_name).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    bce_loss = torch.nn.BCEWithLogitsLoss()

    # ================ Training Loop =================
    for epoch in range(epochs):
        model.train()
        total_loss = 0
        train_targets, train_predictions = [], []
        
        for i, batch in enumerate(train_loader):
            input_ids_real = batch['input_ids_real'].to(device)
            input_ids_treated = batch['input_ids_treated'].to(device)
            input_ids_control = batch['input_ids_control'].to(device)
            attention_mask_real = batch['attention_mask_real'].to(device)
            attention_mask_treated = batch['attention_mask_treated'].to(device)
            attention_mask_control = batch['attention_mask_control'].to(device)
            targets = batch['targets'].float().to(device)
            
            # fwd pass
            (sentiment_outputs_real, sentiment_outputs_treated, sentiment_outputs_control, 
            riesz_outputs_real, riesz_outputs_treated, riesz_outputs_control) = model(
                input_ids_real,
                input_ids_treated,
                input_ids_control,
                attention_mask_real,
                attention_mask_treated,
                attention_mask_control,
            )

            # Compute tau_hat (estimated average treatment effect (ATE) of the 
            # selected treatment_phrase as estimated by a riesz representation
            # formula with RR computed via our simple implementation of RieszNet
            if running_ate:
                if "epoch_riesz_outputs" not in locals():
                    epoch_riesz_outputs, epoch_sentiment_outputs, epoch_targets = [], [], []
                epoch_riesz_outputs.append(riesz_outputs_real.detach())
                epoch_sentiment_outputs.append(torch.sigmoid(sentiment_outputs_real.detach()))
                epoch_targets.append(targets.detach())

                all_riesz_outputs = torch.cat(epoch_riesz_outputs, dim=0)
                all_sentiment_outputs = torch.cat(epoch_sentiment_outputs, dim=0)
                all_targets = torch.cat(epoch_targets, dim=0)

                tau_hat = torch.mean(all_riesz_outputs * all_sentiment_outputs if estimate_targets_for_ate else all_targets)
            else:
                tau_hat = torch.mean(riesz_outputs_real * (torch.sigmoid(sentiment_outputs_real) if estimate_targets_for_ate else targets))
            
            # Compute losses
            riesz_loss = torch.mean(-2 * (riesz_outputs_treated - riesz_outputs_control) + (riesz_outputs_real ** 2))
            reg_loss = torch.mean((sentiment_outputs_treated - sentiment_outputs_control - tau_hat) ** 2)
            bce = bce_loss(sentiment_outputs_real.squeeze(), targets)
            loss = lambda_bce * bce + lambda_reg * reg_loss + lambda_riesz * riesz_loss

            # backprop the gradients and update the model weights
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            total_loss += loss.item()

            # =======   Logging   ========
            # Compute training metrics
            preds = torch.sigmoid(sentiment_outputs_real).squeeze().detach().cpu().numpy()
            preds = (preds > 0.5).astype(int)
            train_targets.extend(targets.cpu().numpy())
            train_predictions.extend(preds)

            if (i + 1) % log_every == 0:
                train_acc = accuracy_score(train_targets, train_predictions)
                train_f1 = f1_score(train_targets, train_predictions)
                wandb.log({"Train Loss": loss.item(), 
                        "Train Accuracy": train_acc, 
                        "Train F1": train_f1, 
                        f"Tau_Hat_{args.treatment_phrase}": tau_hat.item(),
                        "Batch": i + 1})
                print(
                    f"Epoch {epoch + 1}/{epochs}, "
                    f"Batch {i + 1}/{len(train_loader)}, "
                    f"Loss: {loss.item():.4f}, "
                    f"Accuracy: {train_acc:.4f}, "
                    f"F1: {train_f1:.4f}, "
                    f"Tau_Hat_{args.treatment_phrase}: {tau_hat.item():.4f}"
                )
                
        # ======= Validation Metrics (Log Every Epoch) =======
        model.eval()
        val_targets, val_predictions = [], []
        with torch.no_grad():
            for batch in val_loader:
                input_ids_real = batch['input_ids_real'].to(device)
                attention_mask_real = batch['attention_mask_real'].to(device)
                targets = batch['targets'].float().to(device)
                
                sentiment_output_real = model(input_ids_real, None, None, attention_mask_real, None, None)
                preds = torch.sigmoid(sentiment_output_real).squeeze().cpu().numpy()
                preds = (preds > 0.5).astype(int)
                
                val_targets.extend(targets.cpu().numpy())
                val_predictions.extend(preds)
        
        # Compute validation metrics
        val_acc = accuracy_score(val_targets, val_predictions)
        val_f1 = f1_score(val_targets, val_predictions)
        wandb.log({"Val Accuracy": val_acc, "Val F1": val_f1, "Epoch": epoch + 1})
        print(f"Epoch {epoch + 1}/{epochs} Validation Accuracy: {val_acc:.4f}, F1: {val_f1:.4f}")  
        
        # TODO: add early stopping and model checkpointing 
        # ==== end epoch ====
        
    #TODO: reload best checkpointed model 
    
    # compute outputs for full trianing and val sets at the end and save
    # as csvs with verbose model name to out/
    
    # save final metrics to out/     
        
        
if __name__ == "__main__":
    args = get_default_training_args("base_sent")
    train_causal_sent(args)
    print("Training complete :)")