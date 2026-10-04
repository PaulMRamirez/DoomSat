// ======================================================================
// \title  CfdpGuard.hpp
// \brief  The CFDP uplink's gatekeeper: confines uploads to the uplink directory and commits a WAD on board
//         when cfdpManager's own FIN says it arrived whole (see CfdpGuard.fpp).
// ======================================================================

#ifndef DoomMission_CfdpGuard_HPP
#define DoomMission_CfdpGuard_HPP

#include "DoomMission/Components/CfdpGuard/CfdpGuardComponentAc.hpp"

#include <Os/Mutex.hpp>
#include <Svc/Ccsds/CfdpManager/Types/PduBase.hpp>

namespace DoomMission {

class CfdpGuard final : public CfdpGuardComponentBase {
  public:
    //! Uploads followed at once, from Metadata to FIN: above cfdpManager's MaxSimultaneousRx (5), so an upload
    //! cfdpManager is still receiving is never pushed out by newer ones
    static constexpr FwSizeType MAX_UPLOADS = 8;

    explicit CfdpGuard(const char* const compName);
    ~CfdpGuard();

  private:
    // Up: decides each Metadata; every buffer goes on to cfdpManager, a refused Metadata with its descriptor changed
    void uplinkIn_handler(FwIndexType portNum, Fw::Buffer& fwBuffer) override;
    // Down: watches for the receiver's FIN of an upload it let through; every buffer goes on unchanged
    void downlinkIn_handler(FwIndexType portNum, Fw::Buffer& fwBuffer) override;

    //! What to do with one Metadata PDU (the view behind the descriptor); false to refuse it
    bool admitMetadata(const Fw::Buffer& pdu);
    //! Settle the upload a FIN belongs to (the view behind the descriptor)
    void settleFin(const Fw::Buffer& pdu);

    //! One upload let through, from its Metadata until its FIN
    struct Upload {
        bool used;
        Svc::Ccsds::Cfdp::EntityId srcEid;
        Svc::Ccsds::Cfdp::TransactionSeq seq;
        Svc::Ccsds::Cfdp::EntityId destEid;
        U64 order;        //!< when it was recorded: the oldest goes first when the table is full
        Fw::String dest;  //!< the destination as cfdpManager reads it (MetadataPdu::getDestFilename)
    };
    //! A transaction whose Metadata was refused, so its resends are refused without another event
    struct Refused {
        bool used;
        Svc::Ccsds::Cfdp::EntityId srcEid;
        Svc::Ccsds::Cfdp::TransactionSeq seq;
    };

    Os::Mutex m_lock;  //!< the tables: uplinkIn runs on the radio's receive thread, downlinkIn on cfdpManager's
    Upload m_uploads[MAX_UPLOADS];
    Refused m_refused[MAX_UPLOADS];
    FwSizeType m_refusedNext;
    U64 m_order;
    U32 m_accepted;
    U32 m_refusedCount;
    U32 m_committed;
};

}  // namespace DoomMission

#endif
