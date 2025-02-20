module Main where

import Control.Concurrent
import Control.Exception
import Control.Logging
import Data.Binary
import Data.Binary.Get (getRemainingLazyByteString)
import Data.ByteString.Lazy qualified as BSL
import Data.Int (Int64)
import Data.Text (Text, pack)
import Data.Text.Display
import Debug.Trace (traceWith)
import Network.Socket
import Network.Socket.ByteString.Lazy
import Options.Applicative
import Prelude hiding (log)

newtype HsTOptions = HsTOptions {port :: PortNumber}

optionsParser :: Parser HsTOptions
optionsParser =
  HsTOptions
    <$> option
      auto
      ( long "port"
          <> help "Which port to connect to"
          <> showDefault
          <> value 9000
          <> metavar "PORT"
      )

main :: IO ()
main = withStdoutLogging do
  parsedOptions <-
    execParser
      ( info
          (optionsParser <**> helper)
          ( fullDesc
              <> progDesc "Small UDP dummy client for Mr. T"
              <> header "hs-t - talk to Mr. T as if you were an FPGA"
          )
      )
  getAddrInfo Nothing (Just "localhost") (Just (show parsedOptions.port)) >>= \case
    [] -> putStrLn "error looking up address"
    (addr : _) -> do
      sock <- socket (addrFamily addr) Datagram defaultProtocol
      connect sock (addrAddress addr)
      mainLoop (NoSeries sock Nothing)

newtype SeriesId = SeriesId Word32 deriving (Show, Eq)

instance Display SeriesId where
  displayBuilder (SeriesId n) = "[sid " <> displayBuilder n <> "]"

instance Binary SeriesId where
  put (SeriesId w) = put w
  get = SeriesId <$> get

newtype FrameCount = FrameCount Word32
  deriving stock (Ord, Eq)
  deriving newtype (Show)

instance Display FrameCount where
  displayBuilder (FrameCount n) = "[fc " <> displayBuilder n <> "]"

nextFrame :: FrameCount -> FrameCount
nextFrame (FrameCount n) = FrameCount (n + 1)

instance Binary FrameCount where
  put (FrameCount w) = put w
  get = FrameCount <$> get

newtype PackageCount = PackageCount {unPackageCount :: Word32}
  deriving stock (Eq, Ord)
  deriving newtype (Show, Num)

instance Display PackageCount where
  displayBuilder (PackageCount n) = "[pc " <> displayBuilder n <> "]"

instance Binary PackageCount where
  put (PackageCount w) = put w
  get = PackageCount <$> get

data MainLoopState
  = NoSeries Socket (Maybe SeriesId)
  | InSeries Socket SeriesId FrameCount
  | InFrame
      { sock :: Socket,
        series :: SeriesId,
        totalFrames :: FrameCount,
        currentFrame :: FrameCount,
        currentFrameTotalBytes :: PackageCount,
        currentFrameCurrentByte :: PackageCount
      }

data UdpMessage
  = UdpPing
  | UdpPong (Maybe (SeriesId, FrameCount))
  | UdpPacketRequest {frameNumber :: FrameCount, startByte :: PackageCount}
  | UdpPacketReply
      { frameNumber :: FrameCount,
        startByte :: PackageCount,
        bytesInFrame :: PackageCount,
        payload :: BSL.ByteString
      }
  deriving stock (Show)
  deriving (Display) via (ShowInstance UdpMessage)

instance Binary UdpMessage where
  put UdpPing = put (0 :: Word8)
  put (UdpPacketRequest {frameNumber, startByte}) = put (2 :: Word8) >> put frameNumber >> put startByte
  put _ = error "can only be received, not sent"
  get =
    getWord8 >>= \case
      0 -> pure UdpPing
      1 -> do
        sid <- get
        frameCount <- get
        pure $
          UdpPong
            ( if sid == 0
                then Nothing
                else Just (SeriesId sid, frameCount)
            )
      2 -> UdpPacketRequest <$> get <*> get
      3 -> UdpPacketReply <$> get <*> get <*> get <*> getRemainingLazyByteString
      messageType -> fail ("invalid message with code " <> show messageType)

maxUdpSize :: Int64
maxUdpSize = 65536

udpPingPong :: Socket -> UdpMessage -> IO (Either Text UdpMessage)
udpPingPong sock request =
  try (send sock (encode request)) >>= \case
    Left sendE -> do
      warn $ "couldn't send message: " <> display (sendE :: IOException)
      pure (Left (pack (displayException sendE)))
    Right noBytesSent -> do
      log $ "sent " <> display noBytesSent <> " byte(s)"
      log "waiting for answer"
      try (recv sock maxUdpSize) >>= \case
        Left recvE -> do
          warn $ "couldn't receive answer: " <> display (recvE :: IOException)
          pure (Left (pack (displayException recvE)))
        Right bytesReceived -> do
          log $ "received " <> display (BSL.length bytesReceived) <> " byte(s): " <> pack (show bytesReceived)
          case decodeOrFail bytesReceived of
            Left (_unconsumed, _consumed, decodingError) -> do
              log $ "couldn't decode package: " <> pack decodingError
              pure (Left ("error decoding payload: " <> pack decodingError))
            Right (_unconsumed, _consumed, v) -> do
              log $ "decoded succesfully " <> display v
              pure (Right v)

mainLoop :: MainLoopState -> IO ()
mainLoop state@(NoSeries sock lastSeries) = do
  let localLog t = log $ maybe "no series" display lastSeries <> ": " <> t
  localLog "sending ping"
  udpPingPong sock UdpPing >>= \case
    Right (UdpPong (Just (sid, frameCount))) -> do
      if Just sid == lastSeries
        then do
          localLog "still old series"
          threadDelay (1000 * 1000 * 2)
          mainLoop state
        else do
          localLog $ "switching to series " <> display sid
          mainLoop (InSeries sock sid frameCount)
    Right (UdpPong Nothing) -> do
      localLog "still no series"
      threadDelay (1000 * 1000 * 2)
      mainLoop state
    Right _ -> do
      localLog "decoded something that's not a pong"
      threadDelay (1000 * 1000 * 2)
      mainLoop state
    Left _ -> do
      localLog "error decoding"
      threadDelay (1000 * 1000 * 2)
      mainLoop state
mainLoop state@(InSeries sock seriesId frameCount) = do
  let localLog t = log $ display seriesId <> ": " <> t
  localLog "sending initial packet"
  udpPingPong sock (UdpPacketRequest {frameNumber = FrameCount 0, startByte = PackageCount 0}) >>= \case
    Right (UdpPacketReply {frameNumber, startByte, bytesInFrame, payload}) -> do
      localLog "switching to in frame"
      mainLoop
        ( InFrame
            { sock = sock,
              series = seriesId,
              totalFrames = frameCount,
              currentFrame = FrameCount 0,
              currentFrameTotalBytes = bytesInFrame,
              currentFrameCurrentByte = PackageCount (fromIntegral (BSL.length payload))
            }
        )
    _ -> do
      threadDelay (1000 * 1000 * 2)
      mainLoop state
mainLoop state@(InFrame {sock, series, totalFrames, currentFrame, currentFrameTotalBytes, currentFrameCurrentByte}) =
  let localLog t = log $ display series <> "/" <> display currentFrame <> "/" <> display currentFrameCurrentByte <> ": " <> t
      haveFrameSwitch = currentFrameCurrentByte >= currentFrameTotalBytes
      newCurrentFrame :: FrameCount
      newCurrentFrame = if haveFrameSwitch then nextFrame currentFrame else currentFrame
      newStartByte :: PackageCount
      newStartByte = if currentFrameCurrentByte >= currentFrameTotalBytes then 0 else currentFrameCurrentByte
   in if newCurrentFrame >= totalFrames
        then do
          localLog "we've finished!"
          mainLoop (NoSeries sock (Just series))
        else do
          localLog $ "requesting start byte " <> display newStartByte <> " of frame " <> display newCurrentFrame
          udpPingPong sock (UdpPacketRequest {frameNumber = newCurrentFrame, startByte = newStartByte}) >>= \case
            Right (UdpPacketReply {frameNumber, startByte, bytesInFrame, payload}) ->
              if frameNumber /= newCurrentFrame
                then localLog $ "got " <> display frameNumber <> ", expected " <> display newCurrentFrame
                else
                  if startByte /= newStartByte
                    then localLog $ "got " <> display frameNumber <> ", start byte " <> display startByte <> ", expected " <> display newStartByte
                    else
                      if haveFrameSwitch
                        then do
                          localLog $ "frame switch received " <> display (BSL.length payload) <> " initial bytes"
                          mainLoop
                            ( InFrame
                                { sock = sock,
                                  series = series,
                                  totalFrames = totalFrames,
                                  currentFrame = newCurrentFrame,
                                  currentFrameTotalBytes = bytesInFrame,
                                  currentFrameCurrentByte = fromIntegral (BSL.length payload)
                                }
                            )
                        else do
                          localLog $ display (((unPackageCount currentFrameCurrentByte + fromIntegral (BSL.length payload)) * 100) `div` unPackageCount bytesInFrame) <> "%: received " <> display (BSL.length payload) <> " more bytes"
                          mainLoop
                            ( InFrame
                                { sock = sock,
                                  series = series,
                                  totalFrames = totalFrames,
                                  currentFrame = newCurrentFrame,
                                  currentFrameTotalBytes = bytesInFrame,
                                  currentFrameCurrentByte = currentFrameCurrentByte + fromIntegral (BSL.length payload)
                                }
                            )
            _ -> pure ()
          threadDelay (1000 * 1000 * 2)
          mainLoop state
