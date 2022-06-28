import json
import sys
from typing import TypedDict
import torch
import time

from transformers import GPT2LMHeadModel, GPT2Tokenizer
from transformers.modeling_outputs import CausalLMOutputWithCrossAttentions
from nltk.tokenize import RegexpTokenizer
from numpy import argmax

max_top_next = 20
max_new_tokens = 2

# to find all words that are not special characters
word_tokens = r'\w+'
regexp_tokenizer = RegexpTokenizer(word_tokens)

class Model:

	def __init__(self, model_file_path: str):
		self.tokenizer: GPT2Tokenizer = GPT2Tokenizer.from_pretrained(model_file_path, local_files_only=True, padding_side='left')
		self.model: GPT2LMHeadModel = GPT2LMHeadModel.from_pretrained(model_file_path, local_files_only=True)
		self.cache = dict()

	def predict(self, context: 'tuple[str]') -> list :

		inputs = self.tokenizer.encode(context)
		predictions = self.beam_search(inputs, 4, 10)

		inputs = self.tokenizer.encode(context, return_tensors="pt")
		predictions_classic = self.model.generate(inputs, num_beams=10, max_new_tokens=4, num_return_sequences=10)

		print(predictions_classic)

		return predictions

	def greedy_search(self, inputs: 'list[int]', num_new_tokens: int) -> 'list[int]':

		# Generate previous states
		outputs = self.forward(inputs)

		sequence: 'list[int]' = []
		while len(sequence) < num_new_tokens :
			max = outputs.logits.argmax()
			sequence.append(max.item())
			outputs = self.model.forward(max.unsqueeze(0), past_key_values=outputs.past_key_values, use_cache=True, return_dict=True)
		
		return sequence

	def beam_search(self, inputs: 'list[int]', num_new_tokens: int, num_beams: int) -> torch.Tensor:

		if num_beams < 2:
			return ValueError('num_beams should be greater or equal to 2, otherwise use greedy_search')

		# Generate previous states
		outputs = self.forward(inputs)
		
		# Collect top tokens from logits
		last_logits = outputs.logits[-1]
		top_values, top_indices = last_logits.topk(num_beams)

		# Initialize memory
		sequences = top_indices.unsqueeze(1)
		pkv_mem = [outputs.past_key_values] * num_beams
		probs = top_values.softmax(dim = 0)

		# Recursive beam search
		for iteration in range(num_new_tokens - 1):

			# Prepare past_key_values
			N_LAYERS = 12
			past_key_values = [(torch.empty(0, 12, 3, 64), torch.empty(0, 12, 3, 64))] * N_LAYERS
			for layer, (keys, values) in enumerate(past_key_values):
				keys = torch.cat(tuple(pkv[layer][0] for pkv in pkv_mem), dim=0)
				values = torch.cat(tuple(pkv[layer][1] for pkv in pkv_mem), dim=0)
				past_key_values[layer] = (keys, values)
			
			# Prepare next inputs (end of generated sequences)
			next_inputs = sequences[:, -1].unsqueeze(1)

			# Forward pass
			outputs = self.model.forward(next_inputs, past_key_values=tuple(past_key_values), use_cache=True, return_dict=True)

			# Extract most probable tokens
			last_logits = outputs.logits[:, 0]
			top_values, top_indices = last_logits.topk(num_beams, sorted=True)
			# Multiply probabilities by previous for each line
			new_probs = torch.mul(top_values.softmax(dim = 1), probs.unsqueeze(1))

			# Generate new sequences, their probabilities and past key values
			new_sequences = torch.empty((0, iteration + 2), dtype=torch.int32)
			line_indices = [0] * num_beams
			for i in range(num_beams):
				# Find highest value for all probabilities, knowing that torch.topk also does ordering
				max_sums = [new_probs[i, line_indices[i]].item() for i in range(num_beams)]
				column_index = argmax(max_sums)

				# Form a new sequence with the best token and its corresponding previous sequence
				sequence = torch.cat((sequences[column_index], top_indices[column_index, line_indices[column_index]].unsqueeze(0)))
				new_sequences = torch.cat((new_sequences, sequence.unsqueeze(0)))

				# Update probability products
				probs[i] = new_probs[column_index, line_indices[column_index]]

				# Update past keys and values stack
				new_pkv = []
				for keys, values in outputs.past_key_values:
					layer = keys[column_index].unsqueeze(0), values[column_index].unsqueeze(0)
					new_pkv.append(layer)
				pkv_mem[i] = new_pkv

				# Consider next best element from selected context
				line_indices[column_index] += 1

			sequences = new_sequences

		return sequences

	def forward(self, inputs: list[int]) -> CausalLMOutputWithCrossAttentions:
		
		# Find longest subsequence in cache
		known_inputs = tuple(inputs)
		while known_inputs not in self.cache and len(known_inputs) > 0:
			known_inputs = known_inputs[:-1]

		outputs: CausalLMOutputWithCrossAttentions = self.cache.get(known_inputs)
		past_key_values = outputs.past_key_values if len(known_inputs) > 0 else None

		if past_key_values:
			# Check if full sequence is already known
			if known_inputs == tuple(inputs):
				length_from_start = past_key_values[0][0].size(2)
				if len(inputs) == length_from_start:
					# Return outputs as is
					return outputs
				else:
					# Go back one token to generate logits otherwise
					known_inputs = known_inputs[:-1]
					# Possible improvement: truncate known logits and past_key_values

			# Truncate necessary past_key_values for generation
			n = len(known_inputs)
			past_key_values = tuple(
				(keys[:,:,0:n,:], values[:,:,0:n,:])
				for keys, values in past_key_values
			)

		# Forward pass
		outputs = self.model.forward(torch.tensor(inputs[len(known_inputs):]), past_key_values=past_key_values, use_cache=True, return_dict=True)

		# Save outputs for all previously unknown sequences
		unknown_sequence = tuple(inputs)
		while len(unknown_sequence) >= len(known_inputs) and len(unknown_sequence) > 0:
			if unknown_sequence not in self.cache:
				self.cache.update({unknown_sequence: outputs})
			unknown_sequence = unknown_sequence[:-1]

		return outputs

def trim(text: str) -> str:
	words = regexp_tokenizer.tokenize(text)
	if words:
		return words[0]
	else:
		return ""
		
def write_json(any):
	print(json.dumps(any), flush=True)

def main():
	[pyScriptPath, model_file_path] = sys.argv

	write_json({ "event": "state", "value": "initializing" })
	model = Model(model_file_path)
	write_json({ "event": "state", "value": "ready" })

	while True:
		try:
			x = json.loads(input())
			method = x['method']
			params = x['params']
		except:
			print("invalid input", file=sys.stderr, flush=True)

		if method == 'predict':
			context: str = params['context']
			predictions = model.predict(context)
			# predictions = list(map(trim, predictions))
			# write_json(predictions)
			print(predictions)
		if method == 'exit':
			break

if __name__ == "__main__":
	main()