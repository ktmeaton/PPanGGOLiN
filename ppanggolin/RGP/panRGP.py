#!/usr/bin/env python3
#coding:utf-8

#default libraries
import logging
import argparse
import time
import os
from collections import defaultdict, Counter
import random

#installed libraries
from tqdm import tqdm
import networkx as nx
from rpy2 import robjects
from rpy2.robjects.packages import importr

from scipy.spatial.distance import pdist
from scipy.sparse import csc_matrix
from scipy.cluster.hierarchy import linkage, dendrogram
import colorlover as cl

#local libraries
from ppanggolin.pangenome import Pangenome, Region
from ppanggolin.formats import checkPangenomeInfo, writePangenome
from ppanggolin.utils import mkOutdir, jaccard_similarities

class MatriceNode:
    def __init__(self, state, score, prev, gene):
        self.state = state  # state of the node. 1 for RGP and 0 for not RGP.
        self.score = score if score > 0 else 0  # current score of the node
        self.prev = prev  # previous matriceNode
        self.gene = gene  # gene this node corresponds to

    def changes(self, state, score):
        # state of the node. 1 for RGP and 0 for not RGP.
        self.state = 1 if score >= 0 else 0
        # current score of the node. If the given score is negative, set to 0.
        self.score = score if score >= 0 else 0

def extractRGP(contig, node, ID):
    """
        Extract the region from the given starting node
    """
    new_region = Region(contig.name + "_" + str(ID))
    while node.state:
        new_region.append(node.gene)
        node.state = 0
        node.score = 0
        node = node.prev
        if node is None:#it's the end of the contig and the end of the region.
            break
    return new_region

def rewriteMatrix(contig, matrix, index, persistent, continuity, multi):
    """
        ReWrite the matrice from the given index of the node that started a region.
    """
    prev = matrix[index]
    index += 1
    if index > len(matrix) and contig.is_circular:
        index = 0
    # else the node was the last one of the contig, and there is nothing to do
    if index < len(matrix):
        nextNode = matrix[index]
        nbPerc = 0
        while nextNode.state:  # while the old state is not 0, recompute the scores.
            if nextNode.gene.family.namedPartition == "persistent" and nextNode.gene.family not in multi:
                modif = -pow(persistent, nbPerc)
                nbPerc += 1
            else:
                modif = continuity
                nbPerc = 0

            curr_score = modif + prev.score
            curr_state = 1 if curr_score >= 0 else 0
            # scores can't be negative. If they are, they'll be set to 0.
            matrix[index].changes(curr_state, curr_score)
            index += 1
            if index >= len(matrix):
                if contig.is_circular:
                    index = 0
                else:
                    # else we're at the end of the contig, so there are no more computations. Get out of the loop
                    break

            prev = nextNode
            nextNode = matrix[index]

def initMatrices(contig, persistent_penalty, variable_gain, multi ):
    """initialize the vector of score/state nodes"""
    mat = []
    prev = None
    nbPerc = 0
    for gene in contig.genes:
        if gene.family.namedPartition == "persistent" and gene.family not in multi:
            modif = -pow(persistent_penalty, nbPerc)
            nbPerc += 1
        else:
            modif = variable_gain
            nbPerc = 0

        curr_score = modif + prev.score if prev is not None else modif
        curr_state = 1 if curr_score >= 0 else 0
        prev = MatriceNode(curr_state, curr_score, prev, gene)
        mat.append(prev)

    # if the contig is circular and we're in a rgp state, we need to continue from the "starting" gene until we leave rgp state.
    if contig.is_circular and curr_state:
        # the previous node of the first processed gene is the last node.
        mat[0].prev = prev
        lastNode = prev  # saving the last node that was inserted.
        curr_score = prev.score
        c = 0
        nbPerc = 0
        while curr_state:  # while state is rgp.
            matNode = mat[c]
            if matNode == lastNode:  # then we've parsed the entire contig twice. The whole sequence is a rgp so we're stopping the iteration now, otherwise we'll loop indefinitely
                break

            if matNode.gene.family.namedPartition == "persistent" and matNode.gene.family not in multi:
                modif = -pow(persistent_penalty, nbPerc)
                nbPerc += 1
            else:
                modif = variable_gain
                nbPerc = 0

            curr_score = modif + prev.score
            curr_state = 1 if curr_score >= 0 else 0
            matNode.changes(curr_state, curr_score)
            c += 1
    return mat

def mkRegions(contig, matrix, min_length, min_score, persistent, continuity, multi):
    # processing matrix and 'emptying' it to get the regions.
    def maxIndexNode(lst):
        """gets the last node with the highest score from a list of matriceNode"""
        if isinstance(lst, list):
            # init with the first element of the list
            maxScore = lst[0].score
            maxIndex = 0
            for index, node in enumerate(lst):
                if node.score >= maxScore:
                    maxScore = node.score
                    maxIndex = index
            return (maxScore, maxIndex)
        else:
            raise TypeError("List of matriceNode is expected. The detected type was " + type(lst))

    contigRegions = set()
    val, index = maxIndexNode(matrix)
    c = 0
    while val >= min_score:
        new_region = extractRGP(contig, matrix[index], len(contigRegions))
        new_region.score = val
        if (new_region[0].stop - new_region[-1].start) > min_length:
            contigRegions.add(new_region)
        rewriteMatrix(contig, matrix, index, persistent, continuity, multi)
        val, index = maxIndexNode(matrix)
        c += 1
    return contigRegions

def compute_org_rgp(organism, persistent_penalty, variable_gain, min_length, min_score, multigenics):
    orgRegions = set()
    for contig in organism.contigs:
        if len(contig.genes) != 0:#some contigs have no coding genes...
            ## can definitely multiprocess this part, as not THAT much information is needed...
            matrix = initMatrices(contig, persistent_penalty, variable_gain, multigenics)
            orgRegions |= mkRegions(contig, matrix, min_length, min_score, persistent_penalty, variable_gain, multigenics)

    return orgRegions

def get_multigenics(pangenome, dup_margin):
    """
        Returns the multigenic persistent families of the pangenome graph. A family will be considered multigenic if it is duplicated in more than 5% of the genomes where it is present.
    """
    multigenics = set()
    for fam in pangenome.geneFamilies:
        if fam.namedPartition == "persistent":
            dup=len([genes for org, genes in fam.getOrgDict().items() if len(genes) > 1])
            if (dup / len(fam.organisms)) >= dup_margin:#tot / nborgs >= 1.05
                multigenics.add(fam)
    logging.getLogger().info(f"{len(multigenics)} gene families are defined as being multigenic. (duplicated in more than {dup_margin} of the genomes)")
    return multigenics

def spot_distribution(spots, pangenome, output):
    """takes in spots are a list of sets of rgps"""
    fdistrib = open(output + "/spot_rgp_distribution.tsv","w")
    for rgps in spots:
        fdistrib.write(str(len(rgps)) + "\t" + str(len(getUniqRGP(rgps))) + "\t" + str(round(len(rgps) / len(pangenome.organisms),2)) +"\n")
    fdistrib.close()

def getUniqRGP(rgpList):
        uniqRGP = set()
        for rgp in rgpList:
            z = True
            for seenRgp in uniqRGP:
                if rgp == seenRgp:
                    z = False
                    break
            if z:
                uniqRGP.add(rgp)
        return uniqRGP

def compBorder(border1, border2, overlapping_match, exact_match, set_size):
    if border1[0:exact_match] == border2[0:exact_match]:
        return True
    elif len(border1) == set_size and len(border2) == set_size:
        for ikb in range(1, set_size-overlapping_match+1):
            if border1[0:len(border2[ikb:])] == border2[ikb:]:
                return True
        for ib in range(1, set_size-overlapping_match+1):
            if border1[ib:] == border2[0:len(border1[ib:])]:
                return True
    return False

def checkSim(pairKnownBorder, pairBorderGenes, overlapping_match, exact_match, set_size):
    """ Checks if the two pairs of 'exact_match' first gene families are identical, or eventually if they overlap in an ordered way at least 'overlapping_match'"""
    kbpair = [False, False]
    bpair = [False, False]
    for countkb, kb in enumerate(pairKnownBorder):
        for countb, b in enumerate(pairBorderGenes):
            if compBorder(b, kb, overlapping_match, exact_match, set_size):
                kbpair[countkb] = True
                bpair[countb] = True

    if kbpair[0] and kbpair[1] and bpair[0] and bpair[1]:
        return True
    return False

def makeSpotGraph(rgps, multigenics, output, spot_graph, overlapping_match, set_size, exact_match):
    def addNewNode(g, rgp, borders):
        blocks = str(sorted([[gene.family.ID for gene in borders[0]],[gene.family.ID for gene in borders[1]]], key = lambda x : x[0]))
        g.add_node(blocks)
        try:
            g.nodes[blocks]["nb_rgp"]+=1
            g.nodes[blocks]["rgp"].add(rgp)
        except KeyError:
            g.nodes[blocks]["nb_rgp"] = 1
            g.nodes[blocks]["border1"] = [gene.family for gene in borders[1]]
            g.nodes[blocks]["border0"] = [gene.family for gene in borders[0]]
            g.nodes[blocks]["rgp"] = set([rgp])

    spotGraph = nx.Graph()
    lost = 0
    for rgp in rgps:
        border = rgp.getBorderingGenes(set_size, multigenics)
        if len(border[0]) < set_size or len(border[1]) < set_size:
            lost+=1
        else:
            addNewNode(spotGraph, rgp, border)
    logging.getLogger().info(f"{lost} RGPs were not used as they are on a contig border (or have less than {set_size} persistent gene families until the contig border)")
    nodeList = list(spotGraph.nodes)
    logging.getLogger().info(f"{len(nodeList)} number of different pairs of flanking gene families")
    for i, nodei in enumerate(nodeList[:-1]):
        for nodej in nodeList[i+1:]:
            nodeObji = spotGraph.nodes[nodei]
            nodeObjj = spotGraph.nodes[nodej]
            if checkSim([nodeObji["border0"], nodeObji["border1"]], [nodeObjj["border0"], nodeObjj["border1"]], overlapping_match, exact_match, set_size):
                spotGraph.add_edge(nodei, nodej)

    spots = []
    for comp in nx.algorithms.components.connected_components(spotGraph):
        spots.append([ [], set() ])
        for node in comp:
            spots[-1][1] |= spotGraph.nodes[node]["rgp"]
            spots[-1][0].append([spotGraph.nodes[node]["border1"], spotGraph.nodes[node]["border0"]])

    if spot_graph:
        for node in spotGraph.nodes:
            del spotGraph.nodes[node]["border0"]
            del spotGraph.nodes[node]["border1"]
            del spotGraph.nodes[node]["rgp"]

        nx.readwrite.gexf.write_gexf(spotGraph, output + "/spotGraph.gexf")
    return spots

def makeFlanking(spots, output):
    flankGraph = nx.Graph()
    c = 0
    for borders, rgps in spots:
        flankGraph.add_node(c)
        flankGraph.nodes[c]["nb_rgp"] = len(rgps)
        flankGraph.nodes[c]["nb_organisations"] = len(getUniqRGP(rgps))
        bords = set()
        for border in borders:
            bords.add(frozenset(border[0]))
            bords.add(frozenset(border[1]))
        flankGraph.nodes[c]["borders"] = frozenset(bords)
        c+=1

    nodeList = list(flankGraph.nodes)
    for i, nodei in enumerate(nodeList[:-1]):
        for nodej in nodeList[i+1:]:
            inter = len(flankGraph.nodes[nodei]["borders"] & flankGraph.nodes[nodej]["borders"])
            if inter != 0:
                flankGraph.add_edge(nodei, nodej, nb_bord=inter)
    for node in nodeList:
        del flankGraph.nodes[node]["borders"]
    nx.readwrite.gexf.write_gexf(flankGraph, output + "/flankGraph.gexf")

def detect_hotspots(pangenome, multigenics, output, spot_graph = False, flanking_graph = False, overlapping_match = 2, set_size = 3, exact_match = 1, draw_hotspot = False):
    logging.getLogger().info("Detecting hotspots in the pangenome...")

    spots = makeSpotGraph(pangenome.regions, multigenics, output, spot_graph, overlapping_match, set_size, exact_match)

    nb_above1perc = len([ rgps for _, rgps in spots if len(rgps) > len(pangenome.organisms) * 0.05])
    logging.getLogger().info(f"There are {len(spots)} spots in this pangenome, and {nb_above1perc} of them have RGPs in more than 5% of the organisms.")

    if flanking_graph:
        makeFlanking(spots, output)

    # spot_rgps = [ set(rgps) for _, rgps in spots]#list of rgps, regrouped by spots.
    #spot_distribution(spot_rgps, pangenome, output)
    #ADD : Associate rgps on contig borders to known spots. (using both gene content and bordering gene families?)

    if draw_hotspot:
        draw_spots([spot for spot in spots if len(spot[1]) >= len(pangenome.organisms) * 0.05 and len(getUniqRGP(spot[1])) > max(len(pangenome.organisms) * 0.01, 5) ], output, overlapping_match, exact_match, set_size, multigenics)#TODO: add a parameter to control how much presence is needed for a 'hotspot'

    return spots

def checkParameterLogic(overlapping_match, set_size, exact_match):
    if overlapping_match >= set_size:
        raise Exception(f'--overlapping_match_hotspot ({overlapping_match}) cannot be bigger than (or equal to) --set_size_hotspot ({set_size})')
    if exact_match > set_size:
        raise Exception(f'--exact_match_size_hotspot ({exact_match}) cannot be bigger than --set_size_hotspot ({set_size})')

def makeColorsForFams(fams):
    # potentialColors = cl.to_numeric([ col for val in cl.scales['8'].values() for val2 in val.values() for col in val2 ])
    # random.shuffle(potentialColors)
    famcol = {}
    # if len(fams) < len(potentialColors):#can't display if not (families would have the same color)
    for fam in fams:
        col =  list(random.choices(range(256), k=3))
        famcol[fam] = '#%02x%02x%02x' % (col[0], col[1], col[2])
    return famcol

def countRGPoccurrence(uniqRGPS, currRGP):
    countRGPs = Counter()
    for rgp in currRGP:
        for uniqrgp in uniqRGPS:
            if rgp == uniqrgp:
                countRGPs[uniqrgp] += 1
                break
    return countRGPs

def orderGeneLists(geneLists, ordered_counts, overlapping_match, exact_match, set_size):
    geneLists = lineOrderGeneLists(geneLists, overlapping_match, exact_match, set_size)
    return rowOrderGeneLists(geneLists, ordered_counts)

def rowOrderGeneLists(geneLists, ordered_counts):
    famDict = defaultdict(set)

    for index, genelist in enumerate([genelist[0] for genelist in geneLists]):
        for gene in genelist:
            if hasattr(gene,"family"):
                famDict[gene.family].add(index)
    all_indexes = []
    all_columns = []
    data = []
    for famIndex, RGPindexes in enumerate(famDict.values()):
        all_indexes.extend([famIndex] * len(RGPindexes))
        all_columns.extend(RGPindexes)
        data.extend([1.0]*len(RGPindexes))

    mat_p_a = csc_matrix((data, (all_indexes,all_columns)), shape = (len(famDict),len(geneLists)), dtype='float')
    dist    = pdist(1 - jaccard_similarities(mat_p_a,0).todense())
    hc      = linkage(dist, 'single')

    dendro = dendrogram(hc,no_plot=True)

    new_geneLists = [ geneLists[index] for index in dendro["leaves"]]
    new_ordered_counts = [ ordered_counts[index] for index in dendro["leaves"] ]
    return new_geneLists, new_ordered_counts

def lineOrderGeneLists(geneLists, overlapping_match, exact_match, set_size):
    classified = set([0])#first gene list has the right order
    new_classify = set()
    
    nbloop = 0
    to_classify = set(range(1, len(geneLists)))#the others may (or may not) have it
    while len(to_classify) != 0:
        for classIndex in classified:
            base_border1 = [ gene.family for gene in geneLists[classIndex][1][0] ]
            base_border2 = [ gene.family for gene in geneLists[classIndex][1][1] ]
            for unclassIndex in list(to_classify):
                border1 = [ gene.family for gene in geneLists[unclassIndex][1][0] ]
                border2 = [ gene.family for gene in geneLists[unclassIndex][1][1] ]
                if compBorder(base_border1, border1, overlapping_match, exact_match, set_size) or compBorder(base_border2, border2, overlapping_match, exact_match, set_size):
                    to_classify.discard(unclassIndex)
                    new_classify.add(unclassIndex)
                elif compBorder(base_border2, border1, overlapping_match, exact_match, set_size) or compBorder(base_border1, border2, overlapping_match, exact_match, set_size):
                    geneLists[unclassIndex][0] = geneLists[unclassIndex][0][::-1]#reverse the order of the genes to match the 'reference'
                    to_classify.discard(unclassIndex)
                    new_classify.add(unclassIndex)
        classified = new_classify#the newly classified will help to check the unclassified, the formerly classified are not useful for what remains (if sth remains)
        new_classify = set()
        nbloop+=1
        if nbloop>= 10:
            print("infinit loop !")
            print( [ gene.family.ID for border in geneLists[0][1] for gene in border ])
            print("#######")
            for iden in to_classify:
                print( [ gene.family.ID for border in geneLists[iden][1] for gene in border  ])
            print("####")
            for iden in classified:
                print( [ gene.family.ID for border in geneLists[iden][1] for gene in border  ])
            raise Exception()
    return geneLists

def defineElementsOfInterest(genelist):
    present_EOI = set()
    for gene in genelist:
        if 'RNA' in gene.type:
            present_EOI.add(gene.type)
        if 'integrase' in gene.product.lower():
            present_EOI.add('integrase')
        if gene.name == 'yjhS':
            present_EOI.add('yjhS')
    return present_EOI

def drawCurrSpot(genelists, ordered_counts, famCol, filename):
    rdframes = []
    annotList = []
    partitionColors = {"shell": "#00D860", "persistent":"#F7A507", "cloud":"#79DEFF"}
    importr("genoPlotR")
    dna_seg = robjects.r["dna_seg"]
    annotation = robjects.r["annotation"]
    middle = robjects.r["middle"]

    longest_gene_list = 0

    present_EOI = set()

    for index, GeneList in enumerate(genelists):
        genelist = GeneList[0]
        present_EOI |= defineElementsOfInterest(genelist)
        if len(genelist) > longest_gene_list:
            longest_gene_list = len(genelist)
        if genelist[0].start < genelist[1].start:
            ordered=True
            start = genelist[0].start
        else:
            ordered = False
            start = genelist[0].stop
        df = {'name':[],'start':[],'end':[],'strand':[],'col':[], 'fill':[], "gene_type":[]}
        gene_names = []
        
        for gene in genelist:
            if 'RNA' in gene.type:
                gene_names.append(' ' + gene.product)
            else:
                gene_names.append(' ' + gene.name)
            df['name'].append(gene.ID)
            if ordered:
                if gene.strand == "+":
                    df['start'].append(gene.start - start)
                    df['end'].append(gene.stop - start)
                    df['strand'].append(gene.strand)
                else:
                    df["end"].append(gene.start - start)
                    df["start"].append(gene.stop - start)
                    df["strand"].append(gene.strand)
            else:
                if gene.strand == "+":
                    df["start"].append(abs(gene.stop - start))
                    df['end'].append(abs(gene.start - start))
                    df["strand"].append("-")#we invert it because we reversed the order !
                else:
                    df["start"].append(abs(gene.start - start))
                    df["end"].append(abs(gene.stop - start))
                    df["strand"].append("+")#we invert it because we reversed the order !
            if gene.type == "CDS":
                df['col'].append(partitionColors[gene.family.namedPartition])
                df['fill'].append(famCol[gene.family])
                df["gene_type"].append("side_blocks")
            elif gene.type == "tRNA":
                df['col'].append('#000000')
                df['gene_type'].append("bars")
                df['fill'].append('#000000')
            else:
                df['col'].append('#000000')
                df['gene_type'].append("headless_arrows")
                df['fill'].append('#000000')
        df["name"] = robjects.StrVector(df["name"])
        df["start"] = robjects.IntVector(df["start"])
        df["end"] = robjects.IntVector(df["end"])
        df["strand"] = robjects.StrVector(df["strand"])
        df["col"] = robjects.StrVector(df["col"])
        df["fill"] = robjects.StrVector(df["fill"])
        df["gene_type"] = robjects.StrVector(df["gene_type"])
        dnasegObj = dna_seg(robjects.DataFrame(df))
        annot = annotation(x1 = middle(dnasegObj), text=robjects.StrVector(gene_names), rot = 20)
        annotList.append((f'{GeneList[2].organism.name}, x'+str(ordered_counts[index]), annot))
        rdframes.append((f'{GeneList[2].organism.name}, x'+str(ordered_counts[index]), dnasegObj))
    filename = filename +('_' + "_".join(present_EOI) if len(present_EOI) > 0 else "")
    Rannot = robjects.ListVector(annotList)
    Rdna_segs = robjects.ListVector(rdframes)
    plot_gene_map = robjects.r["plot_gene_map"]
    grdevices = importr('grDevices')
    grdevices.png(file=filename +".png", width = longest_gene_list * 70, height= len(rdframes) * 60)
    plot_gene_map(dna_segs = Rdna_segs, annotations=Rannot, lwd = 4)
    grdevices.dev_off()

def draw_spots(spots, output, overlapping_match, exact_match, set_size, multigenics):
    logging.getLogger().info("Drawing the hotspots of the pangenome")
    bar = tqdm(range(len(spots)), unit = "region")
    for i, spot in enumerate(spots):
        uniqRGPS = frozenset(getUniqRGP(spot[1]))
        Fams = set()
        GeneLists = []

        countUniq = countRGPoccurrence(uniqRGPS, spot[1])

        #order unique rgps by occurrences
        sortedUniqRGPs = sorted(uniqRGPS, key = lambda x : countUniq[x], reverse=True)
        for rgp in sortedUniqRGPs:
            GeneList = []
            borders = rgp.getBorderingGenes(set_size, multigenics)
            minpos = min([ gene.position for border in borders for gene in border ])
            maxpos = max([ gene.position for border in borders for gene in border ])
            GeneList = rgp.contig.genes[minpos:maxpos+1]
            minstart = min([ gene.start for border in borders for gene in border ])
            maxstop = max([ gene.stop for border in borders for gene in border ])
            RNAstoadd = set()
            for rna in rgp.contig.RNAs:
                if rna.start > minstart and rna.start < maxstop:
                    RNAstoadd.add(rna)
            GeneList.extend(RNAstoadd)
            GeneList = sorted(GeneList, key = lambda x : x.start)

            Fams |= { gene.family for gene in GeneList if gene.type == "CDS"}

            GeneLists.append([GeneList, borders, rgp])
        famcol = makeColorsForFams(Fams)
        ordered_counts = sorted(countUniq.values(), reverse = True)
        GeneLists, ordered_counts = orderGeneLists(GeneLists, ordered_counts, overlapping_match, exact_match, set_size)
        fname = output + '/hotspot_' + str(i)
        drawCurrSpot(GeneLists, ordered_counts, famcol, fname)#make R dataframes, and plot them using genoPlotR.
        bar.update()
    bar.close()

def predictRGP(pangenome, output, persistent_penalty = 3, variable_gain = 1, min_length = 3000, min_score = 4, dup_margin = 0.05, spot_graph = False,flanking_graph = False,overlapping_match = 2, set_size = 3, exact_match = 1, draw_hotspot = False, cpu = 1):

    #check that given parameters for hotspot computation make sense
    checkParameterLogic(overlapping_match, set_size, exact_match)
    #check statuses and load info
    checkPangenomeInfo(pangenome, needAnnotations=True, needFamilies=True, needGraph=False, needPartitions = True)

    logging.getLogger().info("Detecting multigenic families...")
    multigenics = get_multigenics(pangenome, dup_margin)
    pangenomeRGP = set()
    logging.getLogger().info("Compute Regions of Genomic Plasticity ...")
    bar = tqdm(pangenome.organisms, unit = "genomes")
    for org in bar:
        pangenomeRGP |= compute_org_rgp(org, persistent_penalty, variable_gain, min_length, min_score, multigenics)
    logging.getLogger().info(f"Predicted {len(pangenomeRGP)} RGP")
    pangenome.addRegions(pangenomeRGP)

    spots = detect_hotspots(pangenome, multigenics, output, spot_graph, flanking_graph,overlapping_match, set_size, exact_match, draw_hotspot)

    #save parameters and save status
    pangenome.parameters["RGP"] = {}
    pangenome.parameters["RGP"]["persistent_penalty"] = persistent_penalty
    pangenome.parameters["RGP"]["variable_gain"] = variable_gain
    pangenome.parameters["RGP"]["min_length"] = min_length
    pangenome.parameters["RGP"]["min_score"] = min_score
    pangenome.parameters["RGP"]["dup_margin"] = dup_margin
    pangenome.parameters["RGP"]["overlapping_match"] = overlapping_match
    pangenome.parameters["RGP"]["set_size"] = set_size
    pangenome.parameters["RGP"]["exact_match"] = exact_match
    pangenome.status['predictedRGP'] = "Computed"

def launch(args):
    pangenome = Pangenome()
    pangenome.addFile(args.pangenome)
    # if args.flanking_graph or args.spot_graph:
    mkOutdir(args.output, args.force)
    predictRGP(pangenome, args.output, args.persistent_penalty, args.variable_gain, args.min_length, args.min_score, args.dup_margin, args.spot_graph, args.flanking_graph, args.overlapping_match_hotspot, args.set_size_hotspot, args.exact_match_size_hotspot, args.draw_hotspots, args.cpu)
    writePangenome(pangenome, pangenome.file, args.force)



def rgpSubparser(subparser):
    parser = subparser.add_parser("rgp", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    optional = parser.add_argument_group(title = "Optional arguments")
    optional.add_argument('--persistent_penalty', required=False, type=int, default=3, help="Penalty score to apply to persistent genes")
    optional.add_argument('--variable_gain', required=False, type=int, default=1, help="Gain score to apply to variable genes")
    optional.add_argument('--min_score', required=False, type=int, default=4, help="Minimal score wanted for considering a region as being a RGP")
    optional.add_argument('--min_length', required=False, type=int, default=3000, help="Minimum length (bp) of a region to be considered a RGP")
    optional.add_argument("--dup_margin", required = False, type=int, default=0.05, help="Minimum ratio of organisms where the family is present in which the family must have multiple genes for it to be considered 'duplicated'" )
    optional.add_argument('-o','--output', required=False, type=str, default="ppanggolin_output"+time.strftime("_DATE%Y-%m-%d_HOUR%H.%M.%S", time.localtime())+"_PID"+str(os.getpid()), help="Output directory")
    optional.add_argument("--spot_graph", required = False, action="store_true", help = "Writes a graph in a .gexf format of pairs of blocks of single copy markers flanking RGPs, supposedly belonging to the same hotspot")
    optional.add_argument("--flanking_graph", required = False, action="store_true", help = "Writes a graph in a .gexf format of common blocks of single copy markers flanking RGPs, supposedly with some common origin")
    optional.add_argument("--draw_hotspots", required=False, action="store_true", help = "Draws a figure representing all of the hotspots syntenies")
    optional.add_argument("--overlapping_match_hotspot", required=False, type=int, default = 2, help="The number of 'missing' persistent genes allowed when comparing flanking genes during hotspot computations")
    optional.add_argument("--set_size_hotspot", required = False, type = int, default = 3, help = "Number of single copy markers to use as flanking genes for a RGP during hotspot computation")
    optional.add_argument("--exact_match_size_hotspot", required = False, type= int, default = 1, help = "Number of perfecty matching flanking single copy markers required to associate RGPs during hotspot computation (Ex: If set to 1, two RGPs are in the same hotspot if both their 1st flanking genes are the same)")
    required = parser.add_argument_group(title = "Required arguments", description = "One of the following arguments is required :")
    required.add_argument('-p','--pangenome',  required=True, type=str, help="The pangenome .h5 file")
    return parser