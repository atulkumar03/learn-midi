#!/usr/bin/python

from decode import *
from soundutils import *
import sys

def origRawPcm():	
	if len(sys.argv) > 1:
		midifile = sys.argv[1]
	else:
		midifile = sample_mid_file

	midievents = midi_to_midievents(midifile)
	rawpcm = midievents_to_rawpcm(midievents)
	return streamcopy(rawpcm)

import pickle
import train_simple as t
_, t.nn.params[:] = pickle.load(open("nn_params.dump"))

def midiEventHook(stream):
	for ev in stream:
		print ev
		if ev[0] == "noteon" and ev[3] == 0:
			ev = list(ev)
			ev[3] = 60 # hack
		yield ev

netMidiEvents = t.midiEventsFromPcmViaNet(t.nn, origRawPcm())
netMidiEvents = midiEventHook(netMidiEvents)
pcmstream = midievents_to_rawpcm(netMidiEvents)

for data in pcmstream:
	play(data)
