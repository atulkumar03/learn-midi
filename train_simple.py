#!/usr/bin/env python -u
# by Albert Zeyer, www.az2000.de
# 2011-05-14

from better_exchook import *
sys.excepthook = better_exchook

# only allow up to 800MB memory
import resource
resource.setrlimit(resource.RLIMIT_DATA, (700 * 1024 * 1024, 800 * 1024 * 1024))

import midi
from decode import *
from soundutils import *

#midistr = streamcopy(midi_to_rawpcm(open(sample_mid_file)))
#mp3str = streamcopy(ffmpeg_to_rawpcm(open(sample_mp3_file)))

AudioSamplesPerSecond = 44100
TicksPerSecond = 100
AudioSamplesPerTick = AudioSamplesPerSecond / TicksPerSecond
MillisecsPerTick = 1000 / TicksPerSecond


from numpy.fft import rfft
import numpy as np
import math

#N_window = AudioSamplesPerTick
N_window = AudioSamplesPerSecond / 10
window = np.blackman(N_window)

def pcm_moved_window(rawpcm):
	prefix_len = (N_window - AudioSamplesPerTick) / 2
	postfix_len = N_window - AudioSamplesPerTick - prefix_len
	
	pos = 0
	rawpcm_len = len(rawpcm.getvalue())
	while pos < rawpcm_len:
		offset = pos - prefix_len
		if offset < 0:
			rawpcm.seek(0)
			data = arrayFromPCMStream(rawpcm, N_window + offset)
			data = np.append(np.zeros(-offset), data)
		else:
			rawpcm.seek(offset * 2) # int16
			data = arrayFromPCMStream(rawpcm, N_window)
		if len(data) < N_window:
			data = np.append(data, np.zeros(N_window - len(data)))
		yield data
		pos += AudioSamplesPerTick

def pcm_to_freqs(rawpcm):
	for fdata in pcm_moved_window(rawpcm):
		freqs = rfft(window * fdata)
		freqs = abs(freqs) ** 2
		freqs = freqs + 1 # for np.log
		freqs = np.log(freqs)
		assert len(freqs) == N_window/2+1
		yield freqs


import pybrain
import pybrain.tools.shortcuts as bs
from pybrain.structure.modules import BiasUnit, SigmoidLayer, LinearLayer, LSTMLayer, SoftmaxLayer
import pybrain.structure.networks as bn
import pybrain.structure.connections as bc
import pybrain.rl.learners.valuebased as bl
import pybrain.datasets.sequential as bd


MIDINOTENUM = 128

print "preparing network ...",
nn = bn.RecurrentNetwork()
nn_in_origaudio = LinearLayer(N_window/2+1, name="audioin") # audio freqs input, mono signal
nn_out_midi = SigmoidLayer(MIDINOTENUM * 2, name="outmidi")

nn.addInputModule(nn_in_origaudio)
nn.addOutputModule(nn_out_midi)

#nn_hidden_in = LinearLayer(20, name="hidden-in")
#nn_hidden_mid = LSTMLayer(20, name="hidden-lstm")
#nn_hidden_out = LinearLayer(5, name="hidden-out")
nn_hidden_in = LSTMLayer(nn_out_midi.indim, name="hidden-in")
nn_hidden_out = SigmoidLayer(nn_out_midi.indim * 2, name="hidden-out")

nn.addModule(nn_hidden_in)
if nn_hidden_out is not nn_hidden_in: nn.addModule(nn_hidden_out)

# IN -> HIDDEN-IN
def linearConnect_inToHidden1():
	for i in xrange(nn_hidden_in.indim - nn_in_origaudio.outdim):
		outSliceFrom = i
		outSliceTo = nn_in_origaudio.outdim + i
		nn.addConnection(bc.LinearConnection(
			nn_in_origaudio, nn_hidden_in,
			outSliceFrom=outSliceFrom, outSliceTo=outSliceTo,
			name="in->hidden " + str(i)))

# HIDDEN-IN -> HIDDEN-OUT
def linearConnect_hidden1ToHidden2():
	N = 5
	for i in xrange(N+1):
		inSliceFrom = i
		inSliceTo = nn_in_origaudio.outdim - N + i
		for j in xrange(N+1):
			outSliceFrom = j
			outSliceTo = nn_in_origaudio.outdim - N + j
			nn.addConnection(bc.LinearConnection(
				nn_hidden_in, nn_hidden_out,
				inSliceFrom=inSliceFrom, inSliceTo=inSliceTo, outSliceFrom=outSliceFrom, outSliceTo=outSliceTo,
				name="hidden-" + str(i) + "-" + str(j)))

# HIDDEN-OUT -> OUT
def linearConnect_hidden2ToOut():
	nn.addConnection(bc.LinearConnection(nn_hidden_out, nn_out_midi, name="hidden->out"))

def linearConnect_recurrentBack():
	#nn.addRecurrentConnection(bc.FullConnection(nn_hidden_out, nn_hidden_in, name="recurrent_conn"))
	nn.addRecurrentConnection(bc.LinearConnection(nn_hidden_out, nn_hidden_in, name="recurrent_conn1"))
	nn.addRecurrentConnection(bc.LinearConnection(nn_out_midi, nn_hidden_in, name="recurrent_conn2"))

# NOTE: use the linearConnect_* functions -- or these:
nn.addConnection(bc.FullConnection(nn_in_origaudio, nn_hidden_in, name = "in_c0"))
nn.addConnection(bc.FullConnection(nn_hidden_in, nn_hidden_out, name = "hidden"))
nn.addConnection(bc.FullConnection(nn_hidden_out, nn_out_midi, name = "out_c0"))
nn.addRecurrentConnection(bc.FullConnection(nn_hidden_out, nn_hidden_in, name="recurrent_conn1"))
nn.addRecurrentConnection(bc.FullConnection(nn_out_midi, nn_hidden_in, name="recurrent_conn2"))

nn.sortModules()
print "done"



class AudioIn:
	def __init__(self):
		self.stream = None
	def load(self, filename):
		self.pcmStream = streamcopy(ffmpeg_to_rawpcm(open(filename)))
	def getSamples(self, num):
		return arrayFromPCMStream(self.pcmStream, num)
		
audioIn = AudioIn()

def midistates_to_midievents(midistate_seq, oldMidiKeysState = (False,) * MIDINOTENUM):
	for midiKeysState, midiKeysVelocity in midistate_seq:
		assert len(midiKeysState) == MIDINOTENUM
		assert len(midiKeysVelocity) == MIDINOTENUM
		for note,oldstate,newstate,velocity in izip(count(), oldMidiKeysState, midiKeysState, midiKeysVelocity):
			if not oldstate and newstate:
				yield ("noteon", 0, note, max(0, min(127, int(round(velocity)))))
			elif oldstate and not newstate:
				yield ("noteoff", 0, note)
		yield ("play", MillisecsPerTick)		
		oldMidiKeysState = tuple(midiKeysState)

class MidiSampler:
	def __init__(self):
		self.midiKeysState = [False] * MIDINOTENUM
		self.oldMidiKeysState = tuple(self.midiKeysState)
		self.midiKeysVelocity = [0.0] * MIDINOTENUM
		self.midiEventStream = []
		self.pcmStream = midievents_to_rawpcm(self.midiEventStream)		
	def tick(self):
		self.midiEventStream += list(midistates_to_midievents([(self.midiKeysState, self.midiKeysVelocity)], self.oldMidiKeysState))
		self.oldMidiKeysState = tuple(self.midiKeysState)
	def getSamples(self, num):
		return arrayFromPCMStream(self.stream, num)

midiSampler = MidiSampler()

def audioSamplesAsNetInput(audioSamples):
	audioSamples = map(lambda s: float(s) / 2**15, audioSamples)
	return sum(audioSamples) / len(audioSamples)

def getAudioIn_netInput():
	return audioSamplesAsNetInput(audioIn.getSamples(AudioSamplesPerTick))

def interpretOutMidiKeys(lastMidiKeysState, vec):
	newState = list(lastMidiKeysState)
	assert len(vec) == MIDINOTENUM
	for i in xrange(MIDINOTENUM):
		if vec[i] < 0.3 and lastMidiKeysState[i]:
			newState[i] = False
		elif vec[i] > 0.7 and not lastMidiKeysState[i]:
			newState[i] = True
	return newState

def readMidiKeys_netOutput(vec): pass

def interpretOutMidiKeyVelocities(vec):
	return list(vec)

def readMidiKeyVelocities_netOutput(vec): pass

def getCurMidiKeyVelocities_netInput():
	return list(midiSampler.midiKeysVelocity)

def midiKeysAsNetInput(midiKeysState):
	return map(lambda k: 1.0 if k else 0.0, midiKeysState)

def getCurMidiKeys_netInput():
	return midiKeysAsNetInput(midiSampler.midiKeysState)

def getMidiSamplerAudio_netInput():
	return audioSamplesAsNetInput(midiSampler.getSamples(AudioSamplesPerTick))

def midiVelAsNetInput(vel):
	return vel / 128.0

def interpretOutVel(vel):
	return min(128.0, max(0.0, vel * 128.0))

def interpretNetOutSeq(outseq):
	lastMidiKeysState = [False] * MIDINOTENUM
	for vec in outseq:
		assert len(vec) == 2 * MIDINOTENUM
		netOutKeys = vec[:MIDINOTENUM]
		netOutVels = vec[MIDINOTENUM:]
		keyState = interpretOutMidiKeys(lastMidiKeysState, netOutKeys)
		vels = map(interpretOutVel, netOutVels)
		yield keyState, vels
		lastMidiKeysState = keyState

def pcmStreamToNetSeq(nn, pcm_stream):
	nn.reset()
	oldforget = nn.forget
	nn.forget = True
	for freqs in pcm_to_freqs(pcm_stream):
		outvec = nn.activate(freqs)
		yield outvec
	nn.forget = oldforget

def midiEventsFromPcmViaNet(nn, pcm_stream):
	return \
		midistates_to_midievents(
		interpretNetOutSeq(
		pcmStreamToNetSeq(
			nn, pcm_stream)))
	

import pybrain.supervised as bt
from numpy.random import normal
import random
from itertools import *
import os

def normal_only_pos(mean, std):
	y = normal(mean, std)
	if y < 0.0: y = -y
	return y

def normal_limit(mean, std, min, max):
	y = normal(mean, std)
	if y < min: y = 2 * min - y
	elif y > max: y = 2 * max - y
	if y < min or y > max: y = mean
	return y

def random_note():
	return int(round(normal_limit(MIDINOTENUM/2, std=MIDINOTENUM/4, min=0, max=MIDINOTENUM-1)))

def random_note_vel():
	return normal_only_pos(48, std=25)
	
def random_note_time():
	return int(round(normal_only_pos(200, std=500))) + 1

def generate_random_midistate_seq(millisecs):
	midiKeysState = [0] * MIDINOTENUM # or millisecs time of holding
	midiKeysVelocity = [0.0] * MIDINOTENUM
	for i in xrange(TicksPerSecond * millisecs / 1000):
		midiKeysState = map(lambda k: k - MillisecsPerTick, midiKeysState)
		
		numdown = len([k for k in midiKeysState if k >= 0])
		numdown_good = int(round(normal_only_pos(1, 4)))
		for _ in xrange(random.randint(0, max(numdown_good - numdown, 0))):
			note = random_note()
			midiKeysState[note] = random_note_time()
			midiKeysVelocity[note] = random_note_vel()

		midiKeysVelocity = map(lambda (s,v): v if s >= 0 else 0.0, izip(midiKeysState, midiKeysVelocity))
		yield (map(lambda s: s >= 0, midiKeysState), midiKeysVelocity)

def generate_silent_midistate_seq(millisecs):
	midiKeysState = [False] * MIDINOTENUM
	midiKeysVelocity = [0.0] * MIDINOTENUM
	for i in xrange(TicksPerSecond * millisecs / 1000):
		yield (midiKeysState, midiKeysVelocity)


def generate_seq(maxtime):
	millisecs = maxtime #random.randint(1,20) * 1000
	midistate_seq = list(generate_random_midistate_seq(millisecs))
	midievents_seq = list(midistates_to_midievents(midistate_seq))
	pcm_stream = streamcopy(midievents_to_rawpcm(midievents_seq))
	
	delaytime = 10
	# add delaytime ms silence at beginning so that the NN can operate a bit on the data
	midistate_seq = list(generate_silent_midistate_seq(delaytime)) + midistate_seq
	# add delaytime ms silence at ending (chr(0)*2 for int16(0))
	pcm_stream.seek(0, os.SEEK_END)
	pcm_stream.write(chr(0) * 2 * (AudioSamplesPerSecond * delaytime / 1000))
	pcm_stream.seek(0)
	millisecs += delaytime
	
	for tick, freqs in izip(xrange(TicksPerSecond * millisecs / 1000), pcm_to_freqs(pcm_stream)):
		#print "XXX", tick, pcm_stream.tell(), len(pcm_stream.getvalue()), millisecs, TicksPerSecond * millisecs / 1000
		audio = freqs
		midikeystate,midikeyvel = midistate_seq[tick]
		midikeystate = map(lambda s: 1.0 if s else 0.0, midikeystate)
		midikeyvel = map(midiVelAsNetInput, midikeyvel)
		yield (audio, midikeystate + midikeyvel)

def addSequence(dataset, maxtime):
    dataset.newSequence()
    for i,o in generate_seq(maxtime):
        dataset.addSample(i, o)

def generateData(nseq, maxtime):
    dataset = bd.SequentialDataSet(nn.indim, nn.outdim)
    for i in xrange(nseq): addSequence(dataset, maxtime)
    return dataset



def dump_nn_param_info():
	global nn
	print "len params:", len(nn.params)
	#for m in nn._containerIterator():
	#	print m, ":", len(m.params)
	print "input dim:", nn.indim
	print "output dim:", nn.outdim

if __name__ == '__main__':
	import thread
	def userthread():
		from IPython.Shell import IPShellEmbed
		ipshell = IPShellEmbed()
		ipshell()
	#thread.start_new_thread(userthread, ())
	
	maxtime = 1000
	nseq = 10
	
	import pickle
	try:
		maxtime, nn.params[:] = pickle.load(open("nn_params.dump"))
	except Exception, e:
		print e
		print "ignoring and continuing..."

	import pybrain.optimization as bo
	from pybrain.tools.validation import ModuleValidator
	import pybrain.supervised as bt
	#trainer = bt.BackpropTrainer(nn, learningrate=0.0001, momentum=0.1)
	trainer = bt.RPropMinusTrainer(nn, learningrate=0.0001)
	
	def eval_nn(params):
		global nn, trndata
		nn.reset()
		nn.params[:] = params
		return ModuleValidator.MSE(nn, trndata)
	
	limitParams = (-100.0,100.0)
	supervised = True
	supervisedStepNum = 2
	blackbox = True
	postoptimize = False
	regenerateEpoch = 4
	postOptis = [bo.HillClimber, bo.HillClimber, bo.RandomSearch]
	maxTimeIncErrLimit = 700.0
	maxTimeInc = 1000
	dump_nn_param_info()

	optimizerArgs = {
		"evaluator": eval_nn, "initEvaluable": nn.params,
		"maxEvaluations": 1, "minimize": True,
		}
	optiGeneticArgs = {
		"elitism": True, "eliteProportion": 0.2,
		"populationSize": 20,
	}
	if blackbox:
		method = bo.GA
		#method = bo.ExactNES
		optimizer = method(**dict(optimizerArgs.items() + optiGeneticArgs.items()))
		population = lambda: optimizer.currentpop
		bestparams = lambda: min(optimizer.currentpop, key = eval_nn)
	else:
		population = lambda: [nn.params]
		bestparams = lambda: nn.params
	
	epoch = 0
	tstresults = []
	# carry out the training
	while True:
		print "epoch", epoch
		if epoch % regenerateEpoch == 0:
			print "generating data (maxtime = " + str(maxtime) + ") ...",
			trndata = generateData(nseq = nseq, maxtime = maxtime)
			tstdata = generateData(nseq = nseq, maxtime = maxtime)
			print "done"
		
		if blackbox:
			print "blackbox opti ...",
			optimizer._learnStep()
			besterror = optimizer._bestFound()[1]
			print "done, best error:", besterror

		if limitParams:
			print "limiting params ...",
			for p in population():
				p[:] = map(lambda x: max(limitParams[0], min(limitParams[1], x)), p)
			print "done"
			
		if postoptimize:
			print "post optimize ...",
			optiIndex = 0
			for p in population():
				nn.params[:] = p
				postopti = postOptis[optiIndex](**optimizerArgs)
				postopti._learnStep()
				p[:], besterr = postopti._bestFound()
				optiIndex += 1
				optiIndex %= len(postOptis)
			print "done"
			
		if supervised:
			nn.params[:] = bestparams()
			trnresult = 100. * (ModuleValidator.MSE(nn, trndata))
			tstresult = 100. * (ModuleValidator.MSE(nn, tstdata))
			print "(before trainsteps) train error: %5.2f%%" % trnresult, ",  test error: %5.2f%%" % tstresult

			print "post supervised trainsteps ...",
			for p in population():
				nn.params[:] = p
				trainer.__class__.__init__(trainer, nn) # to recopy params or do whatever else is needed
				trainer.setData(trndata)
				for _ in xrange(supervisedStepNum):
					trainer.train()
				p[:] = nn.params
			print "done"
		
		nn.params[:] = bestparams()
		print "max param:", max(map(abs, nn.params))
		trnresult = 100. * (ModuleValidator.MSE(nn, trndata))
		tstresult = 100. * (ModuleValidator.MSE(nn, tstdata))
		print "train error: %5.2f%%" % trnresult, ",  test error: %5.2f%%" % tstresult

		pickle.dump((maxtime, nn.params), open("nn_params.dump", "w"))
		
		#while supervised and trnresult > 5:
		#	trainer.train()
		#	trnresult = 100. * (ModuleValidator.MSE(nn, trndata))
		#	tstresult = 100. * (ModuleValidator.MSE(nn, tstdata))
		#	print "train error: %5.2f%%" % trnresult, ",  test error: %5.2f%%" % tstresult
			
		tstresults += [tstresult]
		if len(tstresults) > 10: tstresults.pop(0)
		if len(tstresults) >= 10:
			print "test error sum of last 10 episodes:", sum(tstresults)
			if sum(tstresults) < maxTimeIncErrLimit * 10:
				tstresults = []
				maxtime += maxTimeInc
		
		epoch += 1
		