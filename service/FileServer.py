from concurrent import futures

import os
import grpc
import sys
sys.path.append('../generated')
sys.path.append('../utils')
sys.path.append('../proto')
import db
import fileService_pb2_grpc
import fileService_pb2
import heartbeat_pb2_grpc
import heartbeat_pb2
import time
import yaml
import threading
import hashlib
from ShardingHandler import ShardingHandler
from DownloadHelper import DownloadHelper
from lru import LRU

UPLOAD_SHARD_SIZE = 50*1024*1024

class FileServer(fileService_pb2_grpc.FileserviceServicer):
    def __init__(self, hostname, server_port, activeNodesChecker, shardingHandler, superNodeAddress):
        self.serverPort = server_port
        self.serverAddress = hostname+":"+server_port
        self.activeNodesChecker = activeNodesChecker
        self.shardingHandler = shardingHandler
        self.hostname = hostname
        self.lru = LRU(5)
        self.superNodeAddress = superNodeAddress
        
    def UploadFile(self, request_iterator, context):
        print("Inside Server method ---------- UploadFile")
        data=bytes("",'utf-8')
        username, filename = "", ""
        totalDataSize=0
        active_ip_channel_dict = self.activeNodesChecker.getActiveChannels()

        metaData=[]

        if(int(db.get("primaryStatus"))==1):
            print("Inside primary upload")
            currDataSize = 0
            currDataBytes = bytes("",'utf-8')
            seqNo=1
            
            node, node_replica = self.getLeastLoadedNode()

            if(node==-1):
                return fileService_pb2.ack(success=False, message="Error Saving File. No active nodes.")

            for request in request_iterator:
                username, filename = request.username, request.filename
                print("Key is-----------------", username+"_"+filename)
                if(self.fileExists(username, filename)):
                    print("sending neg ack")
                    return fileService_pb2.ack(success=False, message="File already exists for this user. Please rename or delete file first.")
                break
            
            currDataSize+= sys.getsizeof(request.data)
            currDataBytes+=request.data

            for request in request_iterator:

                if((currDataSize + sys.getsizeof(request.data)) > UPLOAD_SHARD_SIZE):
                    self.sendDataToDestination(currDataBytes, node, node_replica, username, filename, seqNo, active_ip_channel_dict[node])
                    metaData.append([node, seqNo, node_replica])
                    currDataBytes = request.data
                    currDataSize = sys.getsizeof(request.data)
                    seqNo+=1
                    node, node_replica = self.getLeastLoadedNode()
                else:
                    currDataSize+= sys.getsizeof(request.data)
                    currDataBytes+=request.data

            if(currDataSize>0):
                self.sendDataToDestination(currDataBytes, node, node_replica, username, filename, seqNo, active_ip_channel_dict[node])
                metaData.append([node, seqNo, node_replica])

            db.saveMetaData(username, filename, metaData)
            self.saveMetadataOnAllNodes(username, filename, metaData)
            return fileService_pb2.ack(success=True, message="Saved")

        else:
            print("Saving the data on my local db")
            sequenceNumberOfChunk = 0
            dataToBeSaved = bytes("",'utf-8')
            for request in request_iterator:
                username, filename, sequenceNumberOfChunk = request.username, request.filename, request.seqNo
                dataToBeSaved+=request.data
            key = username + "_" + filename + "_" + str(sequenceNumberOfChunk)
            db.setData(key, dataToBeSaved)

            if(request.replicaNode!=""):
                print("Sending replication to ", request.replicaNode)
                replica_channel = active_ip_channel_dict[request.replicaNode]
                stub = fileService_pb2_grpc.FileserviceStub(replica_channel)
                response = stub.UploadFile(self.sendDataInStream(dataToBeSaved, username, filename, sequenceNumberOfChunk, ""))

            return fileService_pb2.ack(success=True, message="Saved")

    def sendDataToDestination(self, currDataBytes, node, nodeReplica, username, filename, seqNo, channel):
        if(node==self.serverAddress):
            key = username + "_" + filename + "_" + str(seqNo)
            db.setData(key, currDataBytes)
            if(nodeReplica!=""):
                print("Sending replication to ", nodeReplica)
                active_ip_channel_dict = self.activeNodesChecker.getActiveChannels()
                replica_channel = active_ip_channel_dict[nodeReplica]
                stub = fileService_pb2_grpc.FileserviceStub(replica_channel)
                response = stub.UploadFile(self.sendDataInStream(currDataBytes, username, filename, seqNo, ""))
        else:
            print("Sending the UPLOAD_SHARD_SIZE to node :", node)
            stub = fileService_pb2_grpc.FileserviceStub(channel)
            response = stub.UploadFile(self.sendDataInStream(currDataBytes, username, filename, seqNo, nodeReplica))
            print("Response from uploadFile: ", response.message)

    def sendDataInStream(self, dataBytes, username, filename, seqNo, replicaNode):
        chunk_size = 4000000
        start, end = 0, chunk_size
        while(True):
            chunk = dataBytes[start:end]
            if(len(chunk)==0): break
            start=end
            end += chunk_size
            yield fileService_pb2.FileData(username=username, filename=filename, data=chunk, seqNo=seqNo, replicaNode=replicaNode)

    def DownloadFile(self, request, context):

        print("Inside Download")
        if(int(db.get("primaryStatus"))==1):

            if(self.lru.has_key(request.username + "_" + request.filename)):
                print("Fetching data from Cache")
                CHUNK_SIZE=4000000
                fileName = request.username + "_" + request.filename
                filePath = self.lru[fileName]
                outfile = os.path.join(filePath, fileName)
                
                with open(outfile, 'rb') as infile:
                    while True:
                        chunk = infile.read(CHUNK_SIZE)
                        if not chunk: break
                        yield fileService_pb2.FileData(username=request.username, filename=request.filename, data=chunk, seqNo=1)
            
            else:
                metaData = db.parseMetaData(request.username, request.filename)
                downloadHelper = DownloadHelper(self.hostname, self.serverPort, self.activeNodesChecker)
                data = downloadHelper.getDataFromNodes(request.username, request.filename, metaData)
                print("Sending the data to client")
                chunk_size = 4000000
                start, end = 0, chunk_size
                while(True):
                    chunk = data[start:end]
                    if(len(chunk)==0): break
                    start=end
                    end += chunk_size
                    yield fileService_pb2.FileData(username = request.username, filename = request.filename, data=chunk, seqNo = request.seqNo)
                self.saveInCache(request.username, request.filename, data)

        else:
            key = request.username + "_" + request.filename + "_" + str(request.seqNo)
            print(key)
            data = db.getFileData(key)
            chunk_size = 4000000
            start, end = 0, chunk_size
            while(True):
                chunk = data[start:end]
                if(len(chunk)==0): break
                start=end
                end += chunk_size
                yield fileService_pb2.FileData(username = request.username, filename = request.filename, data=chunk, seqNo = request.seqNo)

    def ListFiles(self, request, context):
        print("List Files Called")

        #Get files in DB and return file names
        return fileService_pb2.FileList(lstFileNames="FILE-LIST")
    
    def fileExists(self, username, filename):
        print("isFile Present", db.keyExists(username + "_" + filename))
        return db.keyExists(username + "_" + filename)
    
    def getLeastLoadedNode(self):
        print("Ready to enter sharding handler")
        node, node_replica = self.shardingHandler.leastUtilizedNode()
        print("Least loaded node is :", node)
        print("Replica node - ", node_replica)
        return node, node_replica

    def MetaDataInfo(self, request, context):
        print("Inside Metadatainfo")
        fileName = request.filename
        seqValues = request.seqValues
        db.saveMetaDataOnOtherNodes(fileName, seqValues)
        ack_message = "Successfully saved the metadata on " + self.serverAddress
        return fileService_pb2.ack(success=False, message=ack_message)

    def saveMetadataOnAllNodes(self, username, filename, metadata):
        print("saveMetadataOnAllNodes")
        active_ip_channel_dict = self.activeNodesChecker.getActiveChannels()
        uniqueFileName = username + "_" + filename
        for ip, channel in active_ip_channel_dict.items():
            if(self.isChannelAlive(channel)):
                print("Active IP", ip)
                stub = fileService_pb2_grpc.FileserviceStub(channel)
                print("STUB->", stub)
                response = stub.MetaDataInfo(fileService_pb2.MetaData(filename=uniqueFileName, seqValues=str(metadata).encode('utf-8')))
                print(response.message)

    def isChannelAlive(self, channel):
        try:
            grpc.channel_ready_future(channel).result(timeout=1)
        except grpc.FutureTimeoutError:
            #print("Connection timeout. Unable to connect to port ")
            return False
        return True
    
    def saveInCache(self, username, filename, data):
        if(len(self.lru.items())>=self.lru.get_size()):
            fileToDel, path = self.lru.peek_last_item()
            os.remove(path+"/"+fileToDel)
        
        self.lru[username+"_"+filename]="cache"
        filePath=os.path.join('cache', username+"_"+filename)
        saveFile = open(filePath, 'wb')
        saveFile.write(data)
        saveFile.close()

    def getClusterStats(self, request, context):
        print("Inside getClusterStats")
        active_ip_channel_dict = self.activeNodesChecker.getActiveChannels()
        total_cpu_usage, total_disk_space, total_used_mem = 0.0,0.0,0.0
        total_nodes = 0
        for ip, channel in active_ip_channel_dict.items():
            if(self.isChannelAlive(channel)):
                stub = heartbeat_pb2_grpc.HearBeatStub(channel)
                stats = stub.isAlive(heartbeat_pb2.NodeInfo(ip="", port=""))
                total_cpu_usage = float(stats.cpu_usage)
                total_disk_space = float(stats.disk_space)
                total_used_mem = float(stats.used_mem)
                total_nodes+=1

        if(total_nodes==0):
            return fileService_pb2.ClusterStats(cpu_usage = str(100.00), disk_space = str(100.00), used_mem = str(100.00))

        return fileService_pb2.ClusterStats(cpu_usage = str(total_cpu_usage/total_nodes), disk_space = str(total_disk_space/total_nodes), used_mem = str(total_used_mem/total_nodes))

    def getLeaderInfo(self, request, context):
        channel = grpc.insecure_channel('{}'.format(self.superNodeAddress))
        stub = fileService_pb2_grpc.FileserviceStub(channel)
        response = stub.getLeaderInfo(fileService_pb2.ClusterInfo(ip = self.hostname, port= self.serverPort, clusterName="team1"))
        print(response.message)



        



    

